# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    purchase_advance_order_id = fields.Many2one(
        'purchase.order',
        string='Advance Deduction for',
        index=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
        help='Set on the vendor bill line that deducts advance payments of this purchase order.')


class AccountMove(models.Model):
    _inherit = 'account.move'

    purchase_advance_deduction_amount = fields.Monetary(
        string='Advance Deducted',
        compute='_compute_purchase_advance_deduction', currency_field='currency_id')
    purchase_advance_can_apply = fields.Boolean(compute='_compute_purchase_advance_deduction')

    @api.depends('invoice_line_ids.purchase_advance_order_id', 'invoice_line_ids.price_subtotal',
                 'invoice_line_ids.purchase_line_id', 'state', 'move_type')
    def _compute_purchase_advance_deduction(self):
        for move in self:
            deduction_lines = move.invoice_line_ids.filtered('purchase_advance_order_id')
            move.purchase_advance_deduction_amount = -sum(deduction_lines.mapped('price_subtotal'))
            orders = move.invoice_line_ids.purchase_line_id.order_id
            move.purchase_advance_can_apply = (
                move.state == 'draft'
                and move.move_type == 'in_invoice'
                and any(o.company_id.purchase_advance_payment_enabled and o.advance_payment_ids for o in orders))

    # -------------------------------------------------------------------------
    # DEDUCTION COMPUTATION
    # -------------------------------------------------------------------------

    def _purchase_advance_compute_deductions(self):
        """ Return {purchase.order: amount} to deduct on this draft vendor bill.

        amount = gross (tax included) amount of the order's lines on this bill
                 x posted advance percentage of the order,
        capped by the advance still available. When the order is fully billed, the whole
        remaining advance is deducted, which absorbs rounding differences of earlier bills.
        """
        self.ensure_one()
        result = {}
        if self.move_type != 'in_invoice':
            return result
        lines = self.invoice_line_ids.filtered(lambda l: l.purchase_line_id and not l.purchase_advance_order_id)
        for order in lines.purchase_line_id.order_id:
            if not order.company_id.purchase_advance_payment_enabled or self.currency_id != order.currency_id:
                continue
            posted_total = order.advance_payment_total
            if order.currency_id.compare_amounts(posted_total, 0.0) <= 0 or order.amount_total <= 0:
                continue
            residual = posted_total - order._get_advance_applied_amount(exclude_moves=self)
            if order.currency_id.compare_amounts(residual, 0.0) <= 0:
                continue
            gross = sum(lines.filtered(lambda l: l.purchase_line_id.order_id == order).mapped('price_total'))
            if order._get_advance_fully_billed():
                amount = residual
            else:
                amount = min(order.currency_id.round(gross * posted_total / order.amount_total), residual)
            amount = min(amount, gross)
            if order.currency_id.compare_amounts(amount, 0.0) > 0:
                result[order] = amount
        return result

    def _purchase_advance_prepare_line_vals(self, order, amount):
        company = order.company_id
        product = company.purchase_advance_product_id
        return {
            'display_type': 'product',
            'name': _('Advance payment deduction - %s', order.name),
            'product_id': product.id,
            'product_uom_id': product.uom_po_id.id or product.uom_id.id,
            'account_id': company.purchase_advance_account_id.id,
            'quantity': 1.0,
            'price_unit': -amount,
            'tax_ids': [(5, 0, 0)],
            'purchase_advance_order_id': order.id,
            'sequence': 9999,
        }

    def _purchase_advance_apply_deductions(self):
        """ (Re)build the advance deduction lines of draft vendor bills. Idempotent. """
        for move in self:
            if move.state != 'draft' or move.move_type != 'in_invoice':
                continue
            commands = [(2, line.id) for line in move.invoice_line_ids.filtered('purchase_advance_order_id')]
            if commands:
                move.write({'invoice_line_ids': commands})
            deductions = move._purchase_advance_compute_deductions()
            if not deductions:
                continue
            move.write({'invoice_line_ids': [
                (0, 0, move._purchase_advance_prepare_line_vals(order, amount))
                for order, amount in deductions.items()
            ]})
            for order, amount in deductions.items():
                move.message_post(body=_(
                    "Advance payment of %(amount)s deducted for %(order)s (%(percent)s%% of the order paid in advance).",
                    amount=move.currency_id.format(amount), order=order._get_html_link(),
                    percent=round(order.advance_payment_percentage, 2)))

    def action_purchase_advance_apply(self):
        if not self.env.user.has_group('account.group_account_invoice'):
            raise UserError(_("Only billing users can apply advance payments."))
        moves = self.filtered(lambda m: m.state == 'draft' and m.move_type == 'in_invoice')
        if not moves:
            raise UserError(_("Advance payments can only be applied on draft vendor bills."))
        moves._purchase_advance_apply_deductions()
        return True

    # -------------------------------------------------------------------------
    # POSTING / RECONCILIATION
    # -------------------------------------------------------------------------

    def _purchase_advance_check_before_post(self):
        for move in self:
            deduction_lines = move.line_ids.filtered('purchase_advance_order_id')
            if not deduction_lines:
                continue
            if move.move_type != 'in_invoice':
                raise UserError(_(
                    "%s: advance deduction lines are only allowed on vendor bills.", move.display_name))
            for order in deduction_lines.purchase_advance_order_id:
                order_lines = deduction_lines.filtered(lambda l: l.purchase_advance_order_id == order)
                prepayment = order.company_id.purchase_advance_account_id
                if order_lines.account_id != prepayment or any(order_lines.tax_ids):
                    raise UserError(_(
                        "%(bill)s: the advance deduction of %(order)s must use the prepayment account "
                        "%(account)s without taxes.",
                        bill=move.display_name, order=order.name, account=prepayment.display_name))
                if move.currency_id != order.currency_id:
                    raise UserError(_(
                        "%(bill)s: the advance deduction of %(order)s must be in %(currency)s.",
                        bill=move.display_name, order=order.name, currency=order.currency_id.name))
                this_bill = -sum(order_lines.mapped('price_subtotal'))
                other_bills = order._get_advance_applied_amount(exclude_moves=move, posted_only=True)
                if order.currency_id.compare_amounts(this_bill + other_bills, order.advance_payment_total) > 0:
                    raise UserError(_(
                        "%(bill)s deducts %(deducted)s of advance payments for %(order)s, but only "
                        "%(available)s of posted advances remain. Post the advance payment or use "
                        "\"Apply Advance Payments\" to recompute the deduction.",
                        bill=move.display_name, order=order.name,
                        deducted=order.currency_id.format(this_bill),
                        available=order.currency_id.format(max(order.advance_payment_total - other_bills, 0.0))))

    def _purchase_advance_reconcile(self):
        """ Match bill deduction lines with the advance payments on the prepayment account. """
        for move in self.filtered(lambda m: m.state == 'posted'):
            for order in move.line_ids.purchase_advance_order_id:
                deduction = move.line_ids.filtered(
                    lambda l: l.purchase_advance_order_id == order and not l.reconciled)
                if not deduction:
                    continue
                payments = order._get_posted_advance_payments().sorted(lambda p: (p.date, p.id))
                advance_lines = payments.move_id.line_ids.filtered(
                    lambda l: l.account_id == deduction.account_id and not l.reconciled
                    and l.balance > 0)
                if advance_lines:
                    (advance_lines + deduction).reconcile()

    def _post(self, soft=True):
        self._purchase_advance_check_before_post()
        posted = super()._post(soft=soft)
        posted._purchase_advance_reconcile()
        return posted
