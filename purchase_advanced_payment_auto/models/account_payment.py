# -*- coding: utf-8 -*-
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    purchase_advance_order_id = fields.Many2one(
        'purchase.order',
        string='Advance for Purchase Order',
        index=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
        check_company=True,
        help='Purchase order this advance payment was registered for.')
    purchase_advance_bill_ids = fields.Many2many(
        'account.move',
        string='Bills Settled by this Advance',
        compute='_compute_purchase_advance_bill_ids')
    purchase_advance_bill_count = fields.Integer(compute='_compute_purchase_advance_bill_ids')

    # -------------------------------------------------------------------------
    # COMPUTE
    # -------------------------------------------------------------------------

    @api.depends('purchase_advance_order_id')
    def _compute_destination_account_id(self):
        super()._compute_destination_account_id()
        for pay in self.filtered('purchase_advance_order_id'):
            account = pay.purchase_advance_order_id.company_id.purchase_advance_account_id
            if account:
                pay.destination_account_id = account

    @api.depends('move_id.line_ids.matched_credit_ids', 'move_id.line_ids.matched_debit_ids')
    def _compute_purchase_advance_bill_ids(self):
        for pay in self:
            bills = self.env['account.move']
            if pay.purchase_advance_order_id and pay.move_id:
                lines = pay._purchase_advance_prepayment_lines()
                partials = lines.matched_credit_ids | lines.matched_debit_ids
                counterparts = partials.credit_move_id | partials.debit_move_id
                bills = (counterparts - lines).move_id.filtered(lambda m: m.is_purchase_document())
            pay.purchase_advance_bill_ids = bills
            pay.purchase_advance_bill_count = len(bills)

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _purchase_advance_prepayment_lines(self):
        """ Journal items of the payment booked on the prepayment account. """
        return self.move_id.line_ids.filtered(
            lambda l: l.payment_id.purchase_advance_order_id
            and l.account_id == l.payment_id.destination_account_id)

    def _seek_for_lines(self):
        # Standard payments only consider receivable/payable accounts as counterpart. Advance
        # payments are booked on an asset (prepayment) account, which must be recognised as the
        # counterpart too, otherwise the synchronisation between payment and entry breaks.
        liquidity_lines, counterpart_lines, writeoff_lines = super()._seek_for_lines()
        if self.purchase_advance_order_id and self.destination_account_id:
            moved = writeoff_lines.filtered(lambda l: l.account_id == self.destination_account_id)
            counterpart_lines |= moved
            writeoff_lines -= moved
        return liquidity_lines, counterpart_lines, writeoff_lines

    # -------------------------------------------------------------------------
    # CONSTRAINTS
    # -------------------------------------------------------------------------

    @api.constrains('purchase_advance_order_id', 'partner_id', 'currency_id', 'payment_type',
                    'is_internal_transfer', 'destination_account_id')
    def _check_purchase_advance_consistency(self):
        for pay in self.filtered('purchase_advance_order_id'):
            order = pay.purchase_advance_order_id
            if pay.is_internal_transfer or pay.payment_type != 'outbound' or pay.partner_type != 'supplier':
                raise ValidationError(_(
                    "Advance payment %s must be an outgoing vendor payment.", pay.display_name))
            if pay.partner_id.commercial_partner_id != order.partner_id.commercial_partner_id:
                raise ValidationError(_(
                    "Advance payment %(payment)s must be paid to the vendor of %(order)s.",
                    payment=pay.display_name, order=order.name))
            if pay.currency_id != order.currency_id:
                raise ValidationError(_(
                    "Advance payment %(payment)s must be in the currency of %(order)s (%(currency)s).",
                    payment=pay.display_name, order=order.name, currency=order.currency_id.name))
            prepayment = order.company_id.purchase_advance_account_id
            if prepayment and pay.destination_account_id != prepayment:
                raise ValidationError(_(
                    "Advance payment %(payment)s must be booked on the prepayment account %(account)s.",
                    payment=pay.display_name, account=prepayment.display_name))

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_post(self):
        res = super().action_post()
        for pay in self.filtered('purchase_advance_order_id'):
            pay.purchase_advance_order_id.message_post(body=Markup(_(
                "Advance payment %(payment)s of %(amount)s posted.")) % {
                'payment': pay._get_html_link(),
                'amount': pay.currency_id.format(pay.amount)})
        return res

    def action_draft(self):
        advances = self.filtered(lambda p: p.purchase_advance_order_id and p.state == 'posted')
        res = super().action_draft()
        for pay in advances:
            pay.purchase_advance_order_id.message_post(body=Markup(_(
                "Advance payment %s was reset to draft; it no longer counts in the advance total.")) % (
                pay._get_html_link(),))
        return res

    def action_view_purchase_advance_bills(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_in_invoice_type')
        bills = self.purchase_advance_bill_ids
        if len(bills) == 1:
            action.update({
                'views': [(self.env.ref('account.view_move_form').id, 'form')],
                'res_id': bills.id,
            })
        else:
            action['domain'] = [('id', 'in', bills.ids)]
        return action
