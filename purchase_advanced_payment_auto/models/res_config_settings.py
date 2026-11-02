# -*- coding: utf-8 -*-
from odoo import fields, models

from .res_company import PREPAYMENT_ACCOUNT_TYPES


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    purchase_advance_payment_enabled = fields.Boolean(
        related='company_id.purchase_advance_payment_enabled', readonly=False)
    purchase_advance_product_id = fields.Many2one(
        related='company_id.purchase_advance_product_id', readonly=False,
        domain="[('type', '=', 'service'), '|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    purchase_advance_account_id = fields.Many2one(
        related='company_id.purchase_advance_account_id', readonly=False,
        domain="[('account_type', 'in', %s), ('deprecated', '=', False), ('company_ids', 'parent_of', company_id)]"
               % (PREPAYMENT_ACCOUNT_TYPES,))

    def set_values(self):
        super().set_values()
        # All related values are written at this point; a failure rolls the save back.
        self.company_id._check_purchase_advance_required_fields()
