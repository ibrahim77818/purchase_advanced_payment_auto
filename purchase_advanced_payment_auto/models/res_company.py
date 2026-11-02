# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Asset account types that may hold vendor prepayments.
PREPAYMENT_ACCOUNT_TYPES = ('asset_prepayments', 'asset_current', 'asset_non_current')


class ResCompany(models.Model):
    _inherit = 'res.company'

    purchase_advance_payment_enabled = fields.Boolean(
        string='Purchase Advance Payments',
        default=False,
        help='Allow registering vendor advance payments from confirmed purchase orders '
             'and deduct them automatically on the related vendor bills.')
    purchase_advance_product_id = fields.Many2one(
        'product.product',
        string='Advance Payment Product',
        domain="[('type', '=', 'service'), '|', ('company_id', '=', False), ('company_id', '=', id)]",
        help='Service product used on the vendor bill line that deducts the advance payment.')
    purchase_advance_account_id = fields.Many2one(
        'account.account',
        string='Prepayment Account',
        domain="[('account_type', 'in', %s), ('deprecated', '=', False), ('company_ids', 'parent_of', id)]"
               % (PREPAYMENT_ACCOUNT_TYPES,),
        help='Asset account debited by advance payments and credited by the deduction '
             'line of the vendor bills. Reconciliation is enabled on it automatically.')

    def _check_purchase_advance_required_fields(self):
        """ Enabled => product and account set. Not an @api.constrains: the settings screen
        writes its related fields to the company one at a time, so the flag can be saved
        before the product and account. It is checked once all values are written
        (res.config.settings.set_values) and again whenever the feature is used. """
        for company in self:
            if company.purchase_advance_payment_enabled and (
                    not company.purchase_advance_product_id or not company.purchase_advance_account_id):
                raise ValidationError(_(
                    "Company %s: an advance payment product and a prepayment account are "
                    "required when purchase advance payments are enabled.", company.name))

    @api.constrains('purchase_advance_product_id', 'purchase_advance_account_id')
    def _check_purchase_advance_configuration(self):
        for company in self:
            product = company.purchase_advance_product_id
            account = company.purchase_advance_account_id
            if product:
                if product.type != 'service':
                    raise ValidationError(_(
                        "The advance payment product %s must be a service.", product.display_name))
                if product.company_id and product.company_id not in company.parent_ids:
                    raise ValidationError(_(
                        "The advance payment product %s belongs to another company.", product.display_name))
            if account:
                if not account.company_ids & company.parent_ids:
                    raise ValidationError(_(
                        "The prepayment account %s belongs to another company.", account.display_name))
                if account.deprecated:
                    raise ValidationError(_(
                        "The prepayment account %s is deprecated.", account.display_name))
                if account.account_type not in PREPAYMENT_ACCOUNT_TYPES:
                    raise ValidationError(_(
                        "The prepayment account %s must be a current, non-current or prepayment "
                        "asset account (not receivable, payable, bank or cash).", account.display_name))

    def _purchase_advance_ensure_reconcilable(self):
        """ Advance payments are matched with bill deductions on the prepayment account. """
        accounts = self.mapped('purchase_advance_account_id').filtered(lambda a: not a.reconcile)
        if accounts:
            accounts.sudo().write({'reconcile': True})

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        companies._purchase_advance_ensure_reconcilable()
        return companies

    def write(self, vals):
        res = super().write(vals)
        if 'purchase_advance_account_id' in vals:
            self._purchase_advance_ensure_reconcilable()
        return res
