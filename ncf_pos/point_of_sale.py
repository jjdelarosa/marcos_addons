# -*- coding: utf-8 -*-
from openerp import models, fields, api, exceptions, tools

import time
import openerp.addons.decimal_precision as dp
from openerp.tools.translate import _
from openerp.osv import osv
from datetime import datetime
import pytz


class PosSession(osv.osv):
    _inherit = ['pos.session', 'mail.thread', 'ir.needaction_mixin']
    _name = "pos.session"

    def _confirm_orders(self, cr, uid, ids, context=None):
        pos_order_obj = self.pool.get('pos.order')
        for session in self.browse(cr, uid, ids, context=context):
            company_id = session.config_id.journal_id.company_id.id
            local_context = dict(context or {}, force_company=company_id)
            order_ids = [order.id for order in session.order_ids if order.state == 'paid']

            move_id = pos_order_obj._create_account_move(cr, uid, session.start_at, session.name,
                                                         session.config_id.journal_id.id, company_id, context=context)

            pos_order_obj._create_account_move_line(cr, uid, order_ids, session, move_id, context=local_context)

            for order in session.order_ids:
                if order.state == 'done':
                    continue
                if order.state in ('draft'):
                    raise exceptions.UserError(
                            _("You cannot confirm all orders of this session, because they have not the 'paid' status"))
                else:
                    pos_order_obj.signal_workflow(cr, uid, [order.id], 'done')

        return True


class PosOrder(models.Model):
    _inherit = ["pos.order", 'mail.thread', 'ir.needaction_mixin']
    _name = 'pos.order'

    def get_real_datetime(self):
        date_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        start = datetime.strptime(date_now, "%Y-%m-%d %H:%M:%S")
        user_tz = self.env.user.tz
        tz = pytz.timezone(user_tz) if user_tz else pytz.utc
        start = pytz.utc.localize(start).astimezone(tz)
        tz_date = start.strftime("%Y-%m-%d %H:%M:%S")
        return tz_date

    def _get_partner_unreconcile(self, operator):
        """
        this method call all unreconcile
        :param operator if operator == &lt; return payment elif oprator = &gt; return invoices:
        :return:
        """

        sql = """
        SELECT "account_move_line"."id", "account_move_line"."amount_residual"
        FROM "account_move_line"
        INNER JOIN "res_partner"  ON "account_move_line"."partner_id" = "res_partner"."id"
        INNER JOIN "account_account"  ON "account_move_line"."account_id" = "account_account"."id"
        WHERE "account_account"."reconcile" = TRUE AND
        "account_move_line"."reconciled" = FALSE AND
        "account_move_line"."amount_residual" {} 0 AND
        "res_partner"."id" = {}
        """.format(operator, self.partner_id.id)

        self.env.cr.execute(sql)
        return self.env.cr.fetchall()

    def _get_partner_unreconcile_invoice(self, operator, invoice_id):

        sql = """
        SELECT "account_move_line"."id", "account_move_line"."amount_residual"
        FROM "account_move_line"
        INNER JOIN "res_partner"  ON "account_move_line"."partner_id" = "res_partner"."id"
        INNER JOIN "account_account"  ON "account_move_line"."account_id" = "account_account"."id"
        WHERE "account_account"."reconcile" = TRUE AND
        "account_move_line"."reconciled" = FALSE AND
        "account_move_line"."amount_residual" {} 0 AND
         "account_move_line"."invoice_id" = {}
        """.format(operator, invoice_id)

        self.env.cr.execute(sql)
        return self.env.cr.fetchall()

    @api.depends("amount_tax", "amount_total", "amount_paid", "amount_return")
    def _amount_all(self):
        for order in self:

            val1 = val2 = 0.0

            cur = order.pricelist_id.currency_id

            for payment in order.statement_ids:
                order.amount_paid += payment.amount
                order.amount_return += (payment.amount < 0 and payment.amount or 0)

            order.amount_paid += order.credit

            for line in order.lines:
                val1 += order._amount_line_tax(line, order.fiscal_position_id)
                val2 += line.price_subtotal

            order.amount_tax = cur.round(val1)
            amount_untaxed = cur.round(val2)
            order.amount_total = order.amount_tax + amount_untaxed

    amount_tax = fields.Float(compute=_amount_all, string='Taxes', digits=0, multi='all')
    amount_total = fields.Float(compute=_amount_all, string='Total', digits=0, multi='all')
    amount_paid = fields.Float(compute=_amount_all, string='Paid', states={'draft': [('readonly', False)]},
                               readonly=True, digits=0, multi='all')
    amount_return = fields.Float(compute=_amount_all, string='Returned', digits=0, multi='all')

    partner_id = fields.Many2one('res.partner', 'Customer', select=1,
                                 states={'draft': [('readonly', False)], 'paid': [('readonly', False)]})
    fiscal_position_id = fields.Many2one('account.fiscal.position', 'Fiscal Position',
                                         domain=[('supplier', '=', False)])
    session_id = fields.Many2one('pos.session', 'Session',
                                 required=True,
                                 select=1,
                                 domain="[('state', '=', 'opened')]",
                                 states={'draft': [('readonly', False)]},
                                 readonly=True)
    reserve_ncf_seq = fields.Char(size=19, copy=False)
    origin = fields.Many2one("pos.order", string="Afecta")
    why_cancel = fields.Char("Concepto de cancelacion")
    state = fields.Selection([('draft', 'New'),
                              ('cancel', 'Cancelled'),
                              ('paid', 'Paid'),
                              ('done', 'Posted'),
                              ('invoiced', 'Invoiced'),
                              ('refund', u"Nota De Crédito"),
                              ('refund_money', u"Nota De Crédito Con Devolucion De Efectivo"),
                              ('wating_refund_money', u'Esperando devolución del pago'),
                              ('draft_refund', u'Devolución en borrador'),
                              ],
                             'Status', readonly=True, copy=False)
    credit = fields.Float(string=u"Créditos aplicados", readonly=True, digits=(16, 2), copy=False)
    credit_type = fields.Selection([('none', 'none'), ('parcial', 'parcial'), ('full', 'full')],
                                   string="Tipo de pago con credito", default="none")
    lines = fields.One2many('pos.order.line', 'order_id', 'Order Lines',
                            states={'draft': [('readonly', False)], 'draft_refund': [('readonly', False)]},
                            readonly=True, copy=True)

    @api.onchange("session_id")
    def onchange_session_id(self):
        self.partner_id = self.session_id.config_id.default_partner_id.id
        self.pricelist_id = self.partner_id.property_product_pricelist.id
        self.fiscal_position_id = self.partner_id.property_account_position_id.id

    @api.onchange("partner_id")
    def _onchange_partner_id(self, part=False):
        if self.partner_id:
            self.fiscal_position_id = self.partner_id.property_account_position_id.id

    @api.onchange("fiscal_position_id")
    def onchange_fiscal_position_id(self):
        if self.fiscal_position_id and self.partner_id.property_account_position_id.id != self.fiscal_position_id:
            self.partner_id.write({"property_account_position_id": self.fiscal_position_id.id})

    @api.model
    def create_from_ui(self, orders):
        context = {u'lang': u'es_DO', u'tz': u'America/Santo_Domingo', u'uid': 1}
        for order in orders:
            if order["data"].get("quotation_type", False):
                return self.with_context(context).action_quotation(order["data"])

        res = super(PosOrder, self).create_from_ui(orders)

        for order_id in res:
            self.browse(order_id).set_reserve_ncf_seq()
            self.env.cr.commit()
            self.browse(order_id).with_context(context).generate_ncf_invoice()
        return res

    @api.model
    def action_paid(self):
        if self.origin:
            self.state = "refund_money"
            self.create_refund_invoice()
            self.action_refund_reconcile()
        elif not self.pos_reference:
            self.set_reserve_ncf_seq()
            self.generate_ncf_invoice()

    @api.model
    def create_refund_invoice(self):
        order = self
        context = dict(self._context)
        invoice_id = order.origin.invoice_id.id
        context.update({'type': 'out_invoice',
                        'active_id': invoice_id,
                        'active_ids': [invoice_id],
                        'search_disable_custom_filters': True,
                        'journal_type': 'sale',
                        'active_model': 'account.invoice',
                        "default_description": order.origin.invoice_id.name
                        })

        refund_inovice = self.env["account.invoice.refund"].with_context(context).create({})
        refund = refund_inovice.invoice_refund()
        refund_invoice_id = self.env["account.invoice"].browse(max(refund["domain"][1][2]))
        refund_invoice_id.write({"invoice_line_ids": [(5, False, False)]})
        inv_line_ref = self.env['account.invoice.line']
        for line in order.lines:
            inv_line = {
                'invoice_id': refund_invoice_id.id,
                'product_id': line.product_id.id,
                'quantity': line.qty * -1,
                'qty_allow_refund': line.qty_allow_refund,
                'account_analytic_id': order._prepare_analytic_account(line),
            }

            invoice_line = inv_line_ref.new(inv_line)
            invoice_line._onchange_product_id()
            invoice_line.invoice_line_tax_ids = [tax.id for tax in invoice_line.invoice_line_tax_ids if
                                                 tax.company_id.id == self.env.user.company_id.id]
            fiscal_position_id = line.order_id.fiscal_position_id
            if fiscal_position_id:
                invoice_line.invoice_line_tax_ids = fiscal_position_id.map_tax(invoice_line.invoice_line_tax_ids)
            invoice_line.invoice_line_tax_ids = [tax.id for tax in invoice_line.invoice_line_tax_ids]
            inv_line = invoice_line._convert_to_write(invoice_line._cache)
            inv_line.update(price_unit=line.price_unit, discount=line.discount)
            inv_line_ref.create(inv_line)

        refund_invoice_id.compute_taxes()
        refund_invoice_id.signal_workflow("invoice_open")

        order.invoice_id = refund_invoice_id.id
        order.create_picking()
        for line in order.lines:
            self.env["pos.order.line"].browse(line.refund_line_ref.id).write(
                    {"qty_allow_refund": line.qty_allow_refund - (line.qty * -1)})

    def set_reserve_ncf_seq(self):
        if not self.partner_id:
            self.partner_id = self.session_id.config_id.default_partner_id.id

        fiscal_type = self.partner_id.property_account_position_id.client_fiscal_type

        if self.amount_total < 0:
            sequence = self.sale_journal.refund_sequence_id
        elif fiscal_type == 'fiscal':
            sequence = self.sale_journal.fiscal_sequence_id
        elif fiscal_type == 'gov':
            sequence = self.sale_journal.gov_sequence_id
        elif fiscal_type == 'special':
            sequence = self.sale_journal.special_sequence_id
        else:
            sequence = self.sale_journal.final_sequence_id

        date_order = self.date_order.split(" ")[0]
        self.reserve_ncf_seq = sequence.with_context(ir_sequence_date=date_order).next_by_id()

    @api.multi
    def refund(self):
        """Create a copy of order for refund order"""
        clone_list = []

        for order in self:
            current_session_ids = self.env['pos.session'].search(
                    [('state', '!=', 'closed'), ('user_id', '=', self._uid)])
            if not current_session_ids:
                raise exceptions.UserError(
                        _('To return product(s), you need to open a session that will be used to register the refund.'))

            clone_id = self.copy({'name': order.name + ' REFUND', 'session_id': current_session_ids[0].id,
                                  'date_order': fields.Date.context_today, 'origin': order.id, "lines": False,
                                  "state": "draft_refund"})

            new_lines = []
            for line in order.lines:
                if not line.qty_allow_refund == 0:
                    ln = (0, False, {'company_id': line.company_id.id,
                                     'name': line.name,
                                     'notice': line.notice,
                                     'product_id': line.product_id.id,
                                     'price_unit': line.price_unit,
                                     'qty': line.qty * -1,
                                     'discount': line.discount,
                                     'order_id': clone_id.id,
                                     'tax_ids': [(6, False, [t.id for t in line.tax_ids])],
                                     'qty_allow_refund': line.qty_allow_refund,
                                     'refund_line_ref': line.id
                                     })

                new_lines.append(ln)

            if not new_lines:
                raise exceptions.UserError("Todos los productos de esta orden ya fueron devueltas!")
            clone_id.write({"lines": new_lines})
            clone_list.append(clone_id)

        abs = {
            'name': _('Devolucion de productos'),
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'pos.order',
            'res_id': clone_list[0].id,
            'view_id': False,
            'context': self._context,
            'type': 'ir.actions.act_window',
            'target': 'current',
        }
        return abs

    def generate_ncf_invoice(self):

        self.write({'state': 'paid'})
        self._cr.execute("update pos_order_line set qty_allow_refund = qty where order_id = {}".format(self.id))
        self.action_invoice()
        self.create_picking()
        self.invoice_id.move_name = self.reserve_ncf_seq
        self.invoice_id.signal_workflow("invoice_open")
        self.action_paid_reconcile()

    @api.model
    def action_paid_reconcile(self):
        for rec in self:
            move_line_ids = []
            for st in rec.statement_ids:
                st.fast_counterpart_creation()

            credit_docs = self._get_partner_unreconcile("<")
            move_line_ids += [r[0] for r in credit_docs]

            debit_docs = self._get_partner_unreconcile_invoice(">", self.invoice_id.id)
            move_line_ids += [r[0] for r in debit_docs]

            self.env["account.move.line.reconcile.writeoff"].with_context(
                    active_ids=move_line_ids).trans_rec_reconcile_partial()

    @api.model
    def action_refund_reconcile(self):
        for rec in self:
            move_line_ids = []
            for st in rec.statement_ids:
                st.fast_counterpart_creation()

            credit_docs = self._get_partner_unreconcile(">")
            move_line_ids += [r[0] for r in credit_docs]

            debit_docs = self._get_partner_unreconcile_invoice("<", self.invoice_id.id)
            move_line_ids += [r[0] for r in debit_docs]

            self.env["account.move.line.reconcile.writeoff"].with_context(
                    active_ids=move_line_ids).trans_rec_reconcile_partial()

    @api.model
    def get_ncf(self, name):
        ncf = False
        while not ncf:
            time.sleep(1)
            ncf = self.search([('pos_reference', '=', name)]).reserve_ncf_seq
            self._cr.commit()
        return {"ncf": ncf}

    @api.model
    def create(self, values):
        context = dict(self._context)
        context["tz"] = self.env.user.tz
        current_session_id = self.env['pos.session'].search([('state', '!=', 'closed'), ('user_id', '=', self._uid)])
        values['name'] = current_session_id.config_id.sequence_id.next_by_id()
        values["date_order"] = self.get_real_datetime()
        return super(models.Model, self).create(values)

    @api.multi
    def add_payment(self, data):
        """Create a new payment for the order"""
        for order in self:
            context = dict(self._context or {})
            statement_line_obj = self.env['account.bank.statement.line']
            property_obj = self.env['ir.property']

            date = data.get('payment_date', time.strftime('%Y-%m-%d'))

            if len(date) > 10:
                timestamp = datetime.strptime(date, tools.DEFAULT_SERVER_DATETIME_FORMAT)
                ts = fields.Datetime.context_timestamp(order, timestamp)
                date = ts.strftime(tools.DEFAULT_SERVER_DATE_FORMAT)
            args = {
                'amount': data['amount'],
                'date': date,
                'name': order.name + ': ' + (data.get('payment_name', '') or ''),
                'partner_id': order.partner_id and self.env["res.partner"]._find_accounting_partner(
                    order.partner_id).id or False,
            }

            args = {
                'amount': data['amount'],
                'date': date,
                'name': order.name + ': ' + (data.get('payment_name', '') or ''),
                'partner_id': order.partner_id and self.env["res.partner"]._find_accounting_partner(
                        order.partner_id).id or False,
            }

            journal_id = data.get('journal', False)
            statement_id = data.get('statement_id', False)
            assert journal_id or statement_id, "No statement_id or journal_id passed to the method!"

            journal = self.env['account.journal'].browse(journal_id)
            # use the company of the journal and not of the current user
            company_cxt = dict(context, force_company=journal.company_id.id)
            account_def = property_obj.get('property_account_receivable_id', 'res.partner')
            args['account_id'] = (order.partner_id and order.partner_id.property_account_receivable_id \
                                  and order.partner_id.property_account_receivable_id.id) or (
                                     account_def and account_def.id) or False

            if not args['account_id']:
                if not args['partner_id']:
                    msg = _('There is no receivable account defined to make payment.')
                else:
                    msg = _('There is no receivable account defined to make payment for the partner: "%s" (id:%d).') % (
                        order.partner_id.name, order.partner_id.id,)
                raise exceptions.UserError(msg)

            context.pop('pos_session_id', False)

            for statement in order.session_id.statement_ids:
                if statement.id == statement_id:
                    journal_id = statement.journal_id.id
                    break
                elif statement.journal_id.id == journal_id:
                    statement_id = statement.id
                    break

            if not statement_id:
                raise exceptions.UserError(_('You have to open at least one cashbox.'))

            args.update({
                'statement_id': statement_id,
                'pos_statement_id': order.id,
                'journal_id': journal_id,
                'ref': order.session_id.name,
            })

            statement_line_obj.create(args)
            return statement_id

    @api.model
    def test_paid(self):
        for order in self:
            order.refresh()
            if order.credit_type == "full":
                return True

            if order.lines and not order.amount_total:
                return True

            if (not order.lines) or (not order.statement_ids) or (
                        abs(order.amount_total - order.amount_paid) > 0.00001):
                return False
        return True

    def action_quotation(self, order):
        sale_ref = self.env['sale.order']
        sale_line_ref = self.env['sale.order.line']
        product_obj = self.env['product.product']

        order_ids = []
        session_id = self.env["pos.session"].browse(order["pos_session_id"])
        partner_id = self.env["res.partner"].browse(order["partner_id"])

        order_dict = {'company_id': session_id.config_id.company_id.id,
                      'partner_id': partner_id.id
                      }

        new_sale_order = sale_ref.new(order_dict)
        new_sale_order.onchange_partner_id()
        sale_order_dict = new_sale_order._convert_to_write(new_sale_order._cache)
        order_id = sale_ref.create(sale_order_dict)

        order_ids.append(order_id)
        order_lines = order_id.order_line.browse([])
        for line in order["lines"]:

            order_line = sale_line_ref.new({
                # 'order_id': order_id.id,
                'product_id': line[2]["product_id"],
                'price_unit': line[2]["price_unit"],
                'product_uom_qty': line[2]["qty"],
                'discount': line[2]["discount"]
            })
            order_line.product_id_change()
            order_line._compute_tax_id()
            order_lines += order_line

        order_id.order_line = order_lines
        order_id.button_dummy()

        if order.get("quotation_type", False) == "print":
            return order_id.print_quotation()
        else:
            return order_id.action_quotation_send()


class PosOrderLine(models.Model):
    _inherit = "pos.order.line"

    qty_allow_refund = fields.Float(string='qty allow refund', digits=dp.get_precision('Product Unit of Measure'),
                                    copy=False, required=False)
    refund_line_ref = fields.Many2one("pos.order.line", string="origin line refund", copy=False)
    note = fields.Char("Nota")


class PosConfig(models.Model):
    _inherit = 'pos.config'

    default_partner_id = fields.Many2one("res.partner", string="Cliente de contado")


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def create_from_ui(self, partner):
        """ create or modify a partner from the point of sale ui.
            partner contains the partner's fields. """
        # image is a dataurl, get the data after the comma
        if partner.get('image', False):
            img = partner['image'].split(',')[1]
            partner['image'] = img

        property_account_position_id = partner.get("property_account_position_id", False)
        if property_account_position_id:
            partner.update({"property_account_position_id": int(property_account_position_id)})

        if partner.get('id', False):  # Modifying existing partner
            partner_id = partner['id']
            del partner['id']
            self.browse(partner_id).write(partner)
        else:
            partner_id = self.create(partner)
            return partner_id.id

        return partner_id