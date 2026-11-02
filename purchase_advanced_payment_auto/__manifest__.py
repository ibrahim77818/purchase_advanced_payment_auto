# -*- coding: utf-8 -*-
{
    'name': 'Purchase Advance Payment Automation',
    'version': '16.0.1.0.0',
    'category': 'Inventory/Purchase',
    'summary': 'Register vendor advance payments from Purchase Orders and deduct them '
               'automatically and proportionally on vendor bills through a prepayment account.',
    'description': """
Purchase Advance Payment Automation
===================================
* Company-level switch under Purchase > Settings > Advance Payment, with a mandatory
  service product and Prepayment (asset) account when enabled.
* "Register Advance Payment" wizard on confirmed purchase orders (amount, journal,
  payment method, date, memo) with strict validation against the remaining
  uninvoiced balance of the order.
* Creates a draft vendor payment (outbound) linked to the order, booked against the
  prepayment account instead of the vendor payable.
* Live metrics on the order: posted advances, advance percentage, applied amount,
  remaining advance and uninvoiced balance.
* Vendor bills created from the order receive a prorated deduction line
  (bill amount x advance percentage), the last bill clears the remaining advance,
  and on posting the deduction is reconciled with the advance payment on the
  prepayment account.
* Full Arabic (ar_001) translation.
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'images': ['static/description/banner.png'],
    'depends': ['purchase'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/purchase_advance_payment_wizard_views.xml',
        'views/res_config_settings_views.xml',
        'views/purchase_order_views.xml',
        'views/account_payment_views.xml',
        'views/account_move_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
