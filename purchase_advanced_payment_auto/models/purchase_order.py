# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .account_payment import ADVANCE_INACTIVE_STATES, ADVANCE_POSTED_STATES


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    advance_payment_enabled = fields.Boolean(related='company_id.purchase_advance_payment_enabled')
    advance_payment_ids = fields.One2many(
        'account.payment', 'purchase_advance_order_id', string='Advance Payments', copy=False)
    advance_deduction_line_ids = fields.One2many(
        'account.move.line', 'purchase_advance_order_id', string='Advance Deduction Lines', copy=False,
        groups='account.group_account_invoice,purchase.group_purchase_user')
    advance_payment_count = fields.Integer(
        string='Advance Payment Count', compute='_compute_advance_payment_count')
    advance_payment_total = fields.Monetary(
        string='Total Advance Payments',
        compute='_compute_advance_payment_total', store=True,
        help='Sum of the posted advance payments of this order. Payments reset to draft or '
             'cancelled are excluded.')
    advance_payment_percentage = fields.Float(
        string='Advance Payment (%)',
        compute='_compute_advance_payment_total', store=True, digits=(16, 2),
        help='Posted advance payments relative to the order total.')
    advance_payment_applied = fields.Monetary(
        string='Advance Deducted on Bills', compute='_compute_advance_payment_balances',
        help='Advance amount already deducted on non-cancelled vendor bills.')
    advance_payment_residual = fields.Monetary(
        string='Remaining Advance', compute='_compute_advance_payment_balances',
        help='Posted advances not yet deducted on a vendor bill.')
    advance_payment_available = fields.Monetary(
        string='Uninvoiced Balance Available for Advance',
        compute='_compute_advance_payment_balances',
        help='Order total minus billed amounts minus advances (draft or posted) not yet '
             'deducted on bills.')

    # -------------------------------------------------------------------------
    # COMPUTE
    # -------------------------------------------------------------------------

    @api.depends('advance_payment_ids')
    def _compute_advance_payment_count(self):
        for order in self:
            order.advance_payment_count = len(order.advance_payment_ids)

    @api.depends('advance_payment_ids.state', 'advance_payment_ids.amount',
                 'advance_payment_ids.currency_id', 'amount_total')
    def _compute_advance_payment_total(self):
        for order in self:
            total = sum(order._get_posted_advance_payments().mapped('amount'))
            order.advance_payment_total = total
            order.advance_payment_percentage = (
                total / order.amount_total * 100.0 if order.amount_total > 0 else 0.0)

    @api.depends('advance_payment_total', 'amount_total', 'currency_id',
                 'advance_payment_ids.state', 'advance_payment_ids.amount',
                 'advance_deduction_line_ids.amount_currency', 'advance_deduction_line_ids.parent_state',
                 'order_line.invoice_lines.price_total', 'order_line.invoice_lines.parent_state')
    def _compute_advance_payment_balances(self):
        for order in self:
            applied = order._get_advance_applied_amount()
            open_advances = sum(order.advance_payment_ids.filtered(
                lambda p: p.state not in ADVANCE_INACTIVE_STATES).mapped('amount'))
            order.advance_payment_applied = applied
            order.advance_payment_residual = order.advance_payment_total - applied
            available = order.amount_total - order._get_advance_billed_gross() - (open_advances - applied)
            order.advance_payment_available = max(order.currency_id.round(available), 0.0)

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _get_posted_advance_payments(self):
        self.ensure_one()
        return self.advance_payment_ids.filtered(lambda p: p.state in ADVANCE_POSTED_STATES)

    def _get_advance_deduction_lines(self, exclude_moves=None):
        """ Deduction lines of non-cancelled bills of this order. """
        self.ensure_one()
        lines = self.sudo().advance_deduction_line_ids.filtered(lambda l: l.parent_state != 'cancel')
        if exclude_moves:
            lines = lines.filtered(lambda l: l.move_id not in exclude_moves)
        return lines

    def _get_advance_applied_amount(self, exclude_moves=None, posted_only=False):
        self.ensure_one()
        lines = self._get_advance_deduction_lines(exclude_moves=exclude_moves)
        if posted_only:
            lines = lines.filtered(lambda l: l.move_id.state == 'posted')
        return self.currency_id.round(sum(
            -l.move_id.direction_sign * l.amount_currency for l in lines))

    def _get_advance_billed_gross(self, exclude_moves=None):
        """ Gross (tax included) amount of this order already on non-cancelled vendor bills,
        before any advance deduction, in the order currency. Credit notes count negatively. """
        self.ensure_one()
        gross = 0.0
        for line in self.order_line.invoice_lines:
            move = line.move_id
            if move.state == 'cancel' or (exclude_moves and move in exclude_moves):
                continue
            sign = -1.0 if move.move_type == 'in_refund' else 1.0
            amount = line.price_total
            if move.currency_id != self.currency_id:
                amount = move.currency_id._convert(
                    amount, self.currency_id, self.company_id, move.invoice_date or move.date or fields.Date.today())
            gross += sign * amount
        return self.currency_id.round(gross)

    def _get_advance_fully_billed(self):
        self.ensure_one()
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        return all(
            float(line.qty_to_invoice) <= 10 ** -precision
            and line.qty_invoiced >= line.product_qty - 10 ** -precision
            for line in self.order_line.filtered(lambda l: not l.display_type))

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_open_advance_payment_wizard(self):
        self.ensure_one()
        self._check_can_register_advance_payment()
        return {
            'name': _('Register Advance Payment'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.advance.payment.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_purchase_order_id': self.id},
        }

    def _check_can_register_advance_payment(self):
        if not self.env.user.has_group('account.group_account_invoice'):
            raise UserError(_("Only billing users can register advance payments."))
        for order in self:
            order.check_access('write')
            if not order.company_id.purchase_advance_payment_enabled:
                raise UserError(_(
                    "Advance payments are not enabled for %s. Enable them in Purchase > "
                    "Configuration > Settings.", order.company_id.name))
            order.company_id._check_purchase_advance_required_fields()
            if order.state not in ('purchase', 'done'):
                raise UserError(_(
                    "Advance payments can only be registered on confirmed purchase orders (%s).",
                    order.name))
            if order.currency_id.compare_amounts(order.advance_payment_available, 0.0) <= 0:
                raise UserError(_(
                    "Purchase order %s has no uninvoiced balance left for an advance payment.",
                    order.name))

    def action_view_advance_payments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_account_payments_payable')
        payments = self.advance_payment_ids
        action['context'] = {
            'default_payment_type': 'outbound',
            'default_partner_type': 'supplier',
            'create': False,
        }
        if len(payments) == 1:
            action.update({
                'views': [(self.env.ref('account.view_account_payment_form').id, 'form')],
                'res_id': payments.id,
            })
        else:
            action['domain'] = [('id', 'in', payments.ids)]
        return action

    def action_create_invoice(self):
        previous_bills = {order.id: order.invoice_ids for order in self}
        action = super().action_create_invoice()
        new_bills = self.env['account.move']
        for order in self:
            new_bills |= order.invoice_ids - previous_bills.get(order.id, self.env['account.move'])
        bills = new_bills.filtered(lambda m: m.move_type == 'in_invoice' and m.state == 'draft')
        if bills:
            bills._purchase_advance_apply_deductions()
        return action

    def button_cancel(self):
        for order in self:
            if order.advance_payment_ids.filtered(lambda p: p.state not in ADVANCE_INACTIVE_STATES):
                raise UserError(_(
                    "Purchase order %s has advance payments. Cancel or reverse them before "
                    "cancelling the order.", order.name))
        return super().button_cancel()
