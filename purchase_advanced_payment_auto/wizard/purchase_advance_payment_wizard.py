# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class PurchaseAdvancePaymentWizard(models.TransientModel):
    _name = 'purchase.advance.payment.wizard'
    _description = 'Register Purchase Advance Payment'

    purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Order', required=True, readonly=True, ondelete='cascade')
    company_id = fields.Many2one(related='purchase_order_id.company_id')
    partner_id = fields.Many2one(related='purchase_order_id.partner_id', string='Vendor')
    currency_id = fields.Many2one(related='purchase_order_id.currency_id')
    order_amount_total = fields.Monetary(related='purchase_order_id.amount_total', string='Order Total')
    advance_payment_total = fields.Monetary(
        related='purchase_order_id.advance_payment_total', string='Already Advanced (Posted)')
    available_amount = fields.Monetary(
        string='Maximum Advance', compute='_compute_available_amount',
        help='Uninvoiced balance of the order not yet covered by an advance payment.')
    amount = fields.Monetary(string='Amount', required=True)
    journal_id = fields.Many2one(
        'account.journal', string='Journal', required=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]")
    available_payment_method_line_ids = fields.Many2many(
        'account.payment.method.line', compute='_compute_available_payment_method_line_ids')
    payment_method_line_id = fields.Many2one(
        'account.payment.method.line', string='Payment Method', required=True,
        compute='_compute_payment_method_line_id', store=True, readonly=False, precompute=True,
        domain="[('id', 'in', available_payment_method_line_ids)]")
    payment_date = fields.Date(string='Payment Date', required=True, default=fields.Date.context_today)
    memo = fields.Char(string='Memo', compute='_compute_memo', store=True, readonly=False, precompute=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        order = self.env['purchase.order'].browse(
            res.get('purchase_order_id') or self.env.context.get('default_purchase_order_id'))
        if order:
            if 'amount' in fields_list and not res.get('amount'):
                res['amount'] = order.advance_payment_available
            if 'journal_id' in fields_list and not res.get('journal_id'):
                journal = self.env['account.journal'].search([
                    ('type', '=', 'bank'), ('company_id', '=', order.company_id.id),
                    ('outbound_payment_method_line_ids', '!=', False),
                ], limit=1)
                res['journal_id'] = journal.id
        return res

    @api.depends('purchase_order_id')
    def _compute_available_amount(self):
        for wizard in self:
            wizard.available_amount = wizard.purchase_order_id.advance_payment_available

    @api.depends('journal_id')
    def _compute_available_payment_method_line_ids(self):
        for wizard in self:
            wizard.available_payment_method_line_ids = wizard.journal_id.outbound_payment_method_line_ids

    @api.depends('journal_id')
    def _compute_payment_method_line_id(self):
        for wizard in self:
            lines = wizard.journal_id.outbound_payment_method_line_ids
            if wizard.payment_method_line_id not in lines:
                wizard.payment_method_line_id = lines[:1]

    @api.depends('purchase_order_id')
    def _compute_memo(self):
        for wizard in self:
            order = wizard.purchase_order_id
            wizard.memo = _('Advance payment - %s', order.name) if order else False

    # -------------------------------------------------------------------------
    # VALIDATION
    # -------------------------------------------------------------------------

    @api.constrains('amount', 'purchase_order_id')
    def _check_amount(self):
        for wizard in self:
            currency = wizard.currency_id
            if currency.compare_amounts(wizard.amount, 0.0) <= 0:
                raise ValidationError(_("The advance payment amount must be strictly positive."))
            available = wizard.purchase_order_id.advance_payment_available
            if currency.compare_amounts(wizard.amount, available) > 0:
                raise ValidationError(_(
                    "The advance payment (%(amount)s) exceeds the remaining uninvoiced balance of "
                    "%(order)s (%(available)s).",
                    amount=currency.format(wizard.amount), order=wizard.purchase_order_id.name,
                    available=currency.format(available)))

    @api.constrains('journal_id', 'payment_method_line_id', 'purchase_order_id')
    def _check_journal(self):
        for wizard in self:
            if wizard.journal_id.company_id != wizard.company_id:
                raise ValidationError(_("The journal must belong to the company of the purchase order."))
            if wizard.journal_id.type not in ('bank', 'cash'):
                raise ValidationError(_("Advance payments must use a bank or cash journal."))
            if wizard.payment_method_line_id not in wizard.journal_id.outbound_payment_method_line_ids:
                raise ValidationError(_(
                    "Payment method %(method)s is not an outgoing method of journal %(journal)s.",
                    method=wizard.payment_method_line_id.display_name, journal=wizard.journal_id.display_name))

    # -------------------------------------------------------------------------
    # ACTION
    # -------------------------------------------------------------------------

    def _prepare_payment_vals(self):
        self.ensure_one()
        order = self.purchase_order_id
        return {
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': order.partner_id.commercial_partner_id.id,
            'amount': self.amount,
            'currency_id': order.currency_id.id,
            'date': self.payment_date,
            'journal_id': self.journal_id.id,
            'payment_method_line_id': self.payment_method_line_id.id,
            'ref': self.memo,
            'purchase_advance_order_id': order.id,
            'destination_account_id': order.company_id.purchase_advance_account_id.id,
            'company_id': order.company_id.id,
        }

    def action_create_payment(self):
        self.ensure_one()
        order = self.purchase_order_id
        order._check_can_register_advance_payment()
        # Re-check against the latest figures (another advance may have been created meanwhile).
        self._check_amount()
        self._check_journal()
        if not order.company_id.purchase_advance_account_id or not order.company_id.purchase_advance_product_id:
            raise UserError(_("Configure the advance payment product and prepayment account first."))
        payment = self.env['account.payment'].with_company(order.company_id).create(self._prepare_payment_vals())
        payment.message_post(body=_("Advance payment created from %s.", order._get_html_link()))
        order.message_post(body=_(
            "Draft advance payment %(payment)s of %(amount)s created.",
            payment=payment._get_html_link(), amount=order.currency_id.format(payment.amount)))
        return {
            'name': _('Advance Payment'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'views': [(self.env.ref('account.view_account_payment_form').id, 'form')],
            'res_id': payment.id,
        }
