# -*- coding: utf-8 -*-
from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged('post_install', '-at_install')
class TestPurchaseAdvancePayment(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Odoo 18 test base: the second company and foreign currency are created on demand.
        cls.company_data_2 = cls.setup_other_company()
        cls.other_currency = cls.setup_other_currency('EUR')
        cls.company = cls.company_data['company']
        cls.env.user.groups_id |= cls.env.ref('purchase.group_purchase_manager')
        cls.prepayment_account = cls.env['account.account'].create({
            'name': 'Vendor Prepayments',
            'code': '139900',
            'account_type': 'asset_prepayments',
            'company_ids': [Command.set(cls.company.ids)],
        })
        cls.advance_product = cls.env['product.product'].create({
            'name': 'Vendor Advance',
            'type': 'service',
            'purchase_method': 'purchase',
        })
        cls.company.write({
            'purchase_advance_payment_enabled': True,
            'purchase_advance_product_id': cls.advance_product.id,
            'purchase_advance_account_id': cls.prepayment_account.id,
        })
        cls.bank_journal = cls.company_data['default_journal_bank']
        cls.tax_15 = cls.env['account.tax'].create({
            'name': 'Purchase 15%',
            'amount': 15.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'company_id': cls.company.id,
        })
        cls.product_ordered = cls.env['product.product'].create({
            'name': 'Billed on ordered qty',
            'type': 'consu',
            'purchase_method': 'purchase',
            'standard_price': 100.0,
            'property_account_expense_id': cls.company_data['default_account_expense'].id,
        })
        cls.product_received = cls.env['product.product'].create({
            'name': 'Billed on received qty',
            'type': 'consu',
            'purchase_method': 'receive',
            'standard_price': 100.0,
            'property_account_expense_id': cls.company_data['default_account_expense'].id,
        })
        cls.vendor = cls.partner_a

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_po(self, lines, currency=None, confirm=True):
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'currency_id': (currency or self.company.currency_id).id,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_qty': qty,
                'price_unit': price,
                'taxes_id': [Command.set(taxes.ids)],
            }) for product, qty, price, taxes in lines],
        })
        if confirm:
            po.button_confirm()
        return po

    def _register_advance(self, po, amount, post=True):
        wizard = self.env['purchase.advance.payment.wizard'].with_context(
            default_purchase_order_id=po.id).create({
                'amount': amount,
                'journal_id': self.bank_journal.id,
            })
        action = wizard.action_create_payment()
        payment = self.env['account.payment'].browse(action['res_id'])
        if post:
            payment.action_post()
        return payment

    def _create_bill(self, po, post=True):
        before = po.invoice_ids
        po.action_create_invoice()
        bill = po.invoice_ids - before
        self.assertEqual(len(bill), 1)
        if post:
            bill.invoice_date = fields.Date.today()
            bill.action_post()
        return bill

    def _prepayment_balance(self, orders):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', self.prepayment_account.id),
            ('parent_state', '=', 'posted'),
            '|', ('move_id', 'in', orders.invoice_ids.ids),
            ('move_id', 'in', orders.advance_payment_ids.move_id.ids),
        ])
        return sum(lines.mapped('balance'))

    def _deduction_line(self, bill):
        return bill.invoice_line_ids.filtered('purchase_advance_order_id')

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def test_01_settings_require_product_and_account(self):
        company = self.company_data_2['company']
        company.purchase_advance_payment_enabled = True
        with self.assertRaises(ValidationError):
            company._check_purchase_advance_required_fields()
        storable = self.env['product.product'].create({'name': 'Goods', 'type': 'consu'})
        with self.assertRaises(ValidationError):
            self.company.purchase_advance_product_id = storable
        with self.assertRaises(ValidationError):
            self.company.purchase_advance_account_id = self.company_data['default_account_payable']
        self.assertTrue(self.prepayment_account.reconcile, "Prepayment account must become reconcilable")

    def test_01b_settings_screen_saves_configuration(self):
        """ Saving the Purchase settings with all three values filled must work
        (related fields are written to the company one by one). """
        company = self.company_data_2['company']
        account = self.env['account.account'].create({
            'name': 'Prepayments 2', 'code': '139901',
            'account_type': 'asset_prepayments', 'company_ids': [Command.set(company.ids)],
        })
        settings = self.env['res.config.settings'].with_company(company).create({
            'purchase_advance_payment_enabled': True,
            'purchase_advance_product_id': self.advance_product.id,
            'purchase_advance_account_id': account.id,
        })
        settings.execute()
        self.assertTrue(company.purchase_advance_payment_enabled)
        self.assertEqual(company.purchase_advance_product_id, self.advance_product)
        self.assertEqual(company.purchase_advance_account_id, account)
        self.assertTrue(account.reconcile)

        # Enabling from the settings screen without the values is refused.
        company_3 = self.env['res.company'].create({'name': 'Company Three'})
        self.env.user.company_ids |= company_3
        settings = self.env['res.config.settings'].with_company(company_3).create({
            'purchase_advance_payment_enabled': True,
        })
        with self.assertRaises(ValidationError):
            settings.execute()

    def test_02_disabled_company_blocks_wizard(self):
        po = self._create_po([(self.product_ordered, 1, 100.0, self.tax_15)])
        self.company.purchase_advance_payment_enabled = False
        with self.assertRaises(UserError):
            po.action_open_advance_payment_wizard()

    def test_03_unconfirmed_po_blocks_wizard(self):
        po = self._create_po([(self.product_ordered, 1, 100.0, self.tax_15)], confirm=False)
        with self.assertRaises(UserError):
            po.action_open_advance_payment_wizard()

    # ------------------------------------------------------------------
    # Wizard and draft payment
    # ------------------------------------------------------------------

    def test_10_wizard_creates_linked_draft_payment(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        self.assertEqual(po.amount_total, 1150.0)
        action = po.action_open_advance_payment_wizard()
        self.assertEqual(action['res_model'], 'purchase.advance.payment.wizard')
        payment = self._register_advance(po, 345.0, post=False)

        self.assertEqual(payment.state, 'draft')
        self.assertEqual(payment.purchase_advance_order_id, po)
        self.assertEqual(payment.partner_id, self.vendor)
        self.assertEqual(payment.payment_type, 'outbound')
        self.assertEqual(payment.partner_type, 'supplier')
        self.assertEqual(payment.destination_account_id, self.prepayment_account)
        self.assertIn(po.name, payment.memo)
        self.assertEqual(po.advance_payment_count, 1)
        # Draft payments do not count as advance yet.
        self.assertEqual(po.advance_payment_total, 0.0)
        self.assertEqual(po.advance_payment_percentage, 0.0)
        # ... but they reserve the uninvoiced balance.
        self.assertEqual(po.advance_payment_available, 805.0)

        # Since Odoo 18 the journal entry is created when the payment is confirmed; it uses the
        # prepayment account as counterpart.
        self.assertFalse(payment.move_id)
        payment.action_post()
        self.assertIn(payment.state, ('in_process', 'paid'))
        self.assertEqual(po.advance_payment_total, 345.0)
        liquidity, counterpart, writeoff = payment._seek_for_lines()
        self.assertEqual(counterpart.account_id, self.prepayment_account)
        self.assertFalse(writeoff)
        self.assertEqual(counterpart.debit, 345.0)

    def test_11_amount_cannot_exceed_uninvoiced_balance(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        with self.assertRaises(ValidationError):
            self._register_advance(po, 1150.01)
        with self.assertRaises(ValidationError):
            self._register_advance(po, 0.0)
        self._register_advance(po, 1000.0, post=False)
        with self.assertRaises(ValidationError):
            self._register_advance(po, 150.01)
        self._register_advance(po, 150.0)
        self.assertEqual(po.advance_payment_available, 0.0)
        with self.assertRaises(UserError):
            po.action_open_advance_payment_wizard()

    def test_12_total_follows_payment_state(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        payment = self._register_advance(po, 345.0)
        self.assertEqual(po.advance_payment_total, 345.0)
        self.assertAlmostEqual(po.advance_payment_percentage, 30.0)

        payment.action_draft()
        self.assertEqual(po.advance_payment_total, 0.0)
        self.assertEqual(po.advance_payment_percentage, 0.0)

        payment.action_post()
        self.assertEqual(po.advance_payment_total, 345.0)
        payment.action_draft()
        payment.action_cancel()
        self.assertEqual(po.advance_payment_total, 0.0)
        self.assertEqual(po.advance_payment_available, 1150.0)

    def test_13_cancel_po_with_advance_is_blocked(self):
        po = self._create_po([(self.product_ordered, 1, 100.0, self.tax_15)])
        self._register_advance(po, 50.0, post=False)
        with self.assertRaises(UserError):
            po.button_cancel()

    def test_14_advance_without_outstanding_account_still_booked(self):
        """ With Accounting installed, Odoo 18 creates no journal entry for payments whose method
        has no outstanding account. Advances fall back to the company's outstanding payments
        account so that they are always booked on the prepayment account. """
        self.bank_journal.outbound_payment_method_line_ids.payment_account_id = False
        po = self._create_po([(self.product_ordered, 1, 100.0, self.tax_15)])
        payment = self._register_advance(po, 50.0, post=False)
        # Simulate the Enterprise behaviour (no fallback set by the core on create).
        payment.outstanding_account_id = False
        payment.action_post()
        self.assertTrue(payment.move_id)
        self.assertEqual(payment.move_id.state, 'posted')
        liquidity, counterpart, writeoff = payment._seek_for_lines()
        self.assertEqual(counterpart.account_id, self.prepayment_account)
        self.assertEqual(liquidity.account_id, payment._get_outstanding_account('outbound'))
        self.assertEqual(po.advance_payment_total, 50.0)

    # ------------------------------------------------------------------
    # Vendor bill deduction and accounting
    # ------------------------------------------------------------------

    def test_20_full_bill_deducts_advance_and_reconciles(self):
        po = self._create_po([
            (self.product_ordered, 6, 100.0, self.tax_15),
            (self.product_ordered, 4, 100.0, self.tax_15),
        ])
        payment = self._register_advance(po, 345.0)
        bill = self._create_bill(po)

        deduction = self._deduction_line(bill)
        self.assertEqual(len(deduction), 1)
        self.assertEqual(deduction.account_id, self.prepayment_account)
        self.assertFalse(deduction.tax_ids)
        self.assertEqual(deduction.product_id, self.advance_product)
        self.assertEqual(deduction.price_subtotal, -345.0)
        # Taxes are computed on the full purchase, unaffected by the deduction.
        self.assertEqual(bill.amount_tax, 150.0)
        self.assertEqual(bill.amount_total, 805.0)
        self.assertEqual(bill.amount_residual, 805.0)

        # Prepayment cleared and matched with the payment.
        self.assertTrue(deduction.reconciled)
        self.assertTrue(payment._purchase_advance_prepayment_lines().reconciled)
        self.assertEqual(payment.purchase_advance_bill_ids, bill)
        self.assertEqual(self._prepayment_balance(po), 0.0)
        self.assertEqual(po.advance_payment_applied, 345.0)
        self.assertEqual(po.advance_payment_residual, 0.0)

        # Vendor payable only carries the net amount still due.
        payable = bill.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable')
        self.assertEqual(payable.balance, -805.0)
        self.assertTrue(bill.currency_id.is_zero(sum(bill.line_ids.mapped('balance'))))

    def test_21_partial_bills_prorate_then_clear_residual(self):
        po = self._create_po([(self.product_received, 10, 100.0, self.tax_15)])
        self._register_advance(po, 460.0)  # 40 %
        self.assertAlmostEqual(po.advance_payment_percentage, 40.0)

        po.order_line.qty_received = 4
        bill1 = self._create_bill(po)
        self.assertEqual(self._deduction_line(bill1).price_subtotal, -184.0)  # 460 x 40 %
        self.assertEqual(bill1.amount_total, 460.0 - 184.0)
        self.assertEqual(po.advance_payment_residual, 276.0)

        po.order_line.qty_received = 10
        bill2 = self._create_bill(po)
        self.assertEqual(self._deduction_line(bill2).price_subtotal, -276.0)
        self.assertEqual(po.advance_payment_residual, 0.0)
        self.assertEqual(self._prepayment_balance(po), 0.0)
        self.assertEqual(sum((bill1 + bill2).mapped('amount_total')), 1150.0 - 460.0)

    def test_22_rounding_absorbed_by_last_bill(self):
        po = self._create_po([(self.product_received, 3, 333.33, self.env['account.tax'])])
        self.assertEqual(po.amount_total, 999.99)
        self._register_advance(po, 100.0)
        deducted = 0.0
        for qty in (1, 2, 3):
            po.order_line.qty_received = qty
            bill = self._create_bill(po)
            deducted -= self._deduction_line(bill).price_subtotal
        self.assertAlmostEqual(deducted, 100.0, places=2)
        self.assertEqual(self._prepayment_balance(po), 0.0)

    def test_23_draft_advance_not_deducted_until_posted(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        payment = self._register_advance(po, 345.0, post=False)
        bill = self._create_bill(po, post=False)
        self.assertFalse(self._deduction_line(bill))
        self.assertTrue(bill.purchase_advance_can_apply)

        payment.action_post()
        bill.action_purchase_advance_apply()
        self.assertEqual(self._deduction_line(bill).price_subtotal, -345.0)
        # Applying again is idempotent.
        bill.action_purchase_advance_apply()
        self.assertEqual(len(self._deduction_line(bill)), 1)
        self.assertEqual(bill.purchase_advance_deduction_amount, 345.0)

    def test_24_cannot_post_bill_beyond_posted_advances(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        payment = self._register_advance(po, 345.0)
        bill = self._create_bill(po, post=False)
        payment.action_draft()
        bill.invoice_date = fields.Date.today()
        with self.assertRaises(UserError):
            bill.action_post()
        # Recomputing removes the deduction since nothing is posted anymore.
        bill.action_purchase_advance_apply()
        self.assertFalse(self._deduction_line(bill))
        bill.action_post()
        self.assertEqual(bill.amount_total, 1150.0)

    def test_25_reset_bill_to_draft_unreconciles(self):
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        payment = self._register_advance(po, 345.0)
        bill = self._create_bill(po)
        bill.button_draft()
        self.assertFalse(payment._purchase_advance_prepayment_lines().reconciled)
        self.assertEqual(po.advance_payment_applied, 345.0)  # still reserved by the draft bill
        bill.button_cancel()
        self.assertEqual(po.advance_payment_applied, 0.0)
        self.assertEqual(po.advance_payment_residual, 345.0)

    def test_26_foreign_currency_order(self):
        currency = self.other_currency
        po = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)], currency=currency)
        payment = self._register_advance(po, 575.0)  # 50 %
        self.assertEqual(payment.currency_id, currency)
        bill = self._create_bill(po)
        deduction = self._deduction_line(bill)
        self.assertEqual(deduction.amount_currency, -575.0)
        self.assertEqual(bill.amount_total, 575.0)
        self.assertTrue(deduction.reconciled)
        self.assertTrue(self.company.currency_id.is_zero(self._prepayment_balance(po)))

    def test_27_combined_bill_for_two_orders(self):
        po1 = self._create_po([(self.product_ordered, 10, 100.0, self.tax_15)])
        po2 = self._create_po([(self.product_ordered, 5, 100.0, self.tax_15)])
        self._register_advance(po1, 115.0)  # 10 %
        self._register_advance(po2, 287.5)  # 50 %
        (po1 | po2).action_create_invoice()
        bill = po1.invoice_ids
        self.assertEqual(bill, po2.invoice_ids)
        deductions = self._deduction_line(bill)
        self.assertEqual(len(deductions), 2)
        by_order = {l.purchase_advance_order_id: l.price_subtotal for l in deductions}
        self.assertEqual(by_order[po1], -115.0)
        self.assertEqual(by_order[po2], -287.5)
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        self.assertTrue(all(deductions.mapped('reconciled')))
        self.assertEqual(self._prepayment_balance(po1 | po2), 0.0)
