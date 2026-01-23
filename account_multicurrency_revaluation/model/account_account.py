from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL


class AccountAccount(models.Model):
    _inherit = "account.account"

    currency_revaluation = fields.Boolean(
        string="Allow Currency Revaluation",
        compute="_compute_currency_revaluation",
        store=True,
        readonly=False,
    )

    _sql_mapping = {
        "balance": "COALESCE(SUM(debit),0) - COALESCE(SUM(credit), 0) as balance",
        "debit": "COALESCE(SUM(debit), 0) as debit",
        "credit": "COALESCE(SUM(credit), 0) as credit",
        "foreign_balance": "COALESCE(SUM(amount_currency), 0) as foreign_balance",
    }

    def init(self):
        # all receivable, payable, Bank and Cash accounts should
        # have currency_revaluation True by default
        res = super().init()
        accounts = self.env["account.account"].search(
            [
                ("account_type", "in", self._get_revaluation_account_types()),
                ("currency_revaluation", "=", False),
                ("include_initial_balance", "=", True),
            ]
        )
        accounts.write({"currency_revaluation": True})
        return res

    def write(self, vals):
        if (
            "currency_revaluation" in vals
            and vals.get("currency_revaluation", False)
            and any([not x for x in self.mapped("include_initial_balance")])
        ):
            raise UserError(
                self.env._(
                    "There is an account that you are editing not having the Bring "
                    "Balance Forward set, the currency revaluation cannot be applied "
                    "on these accounts: \n\t - %s"
                )
                % "\n\t - ".join(
                    self.filtered(lambda x: not x.include_initial_balance).mapped(
                        "name"
                    )
                )
            )
        return super().write(vals)

    def _get_revaluation_account_types(self):
        return [
            "asset_receivable",
            "liability_payable",
            "asset_cash",
            "liability_credit_card",
        ]

    @api.depends("account_type")
    def _compute_currency_revaluation(self):
        for rec in self:
            revaluation_accounts = rec._get_revaluation_account_types()
            if rec.account_type in revaluation_accounts:
                rec.currency_revaluation = True
            else:
                rec.currency_revaluation = False

    def _revaluation_query(self, revaluation_date, start_date=None):
        query = self.env["account.move.line"]._search(
            [
                ("company_id", "in", self.env.companies.ids),
                ("display_type", "not in", ("line_section", "line_note")),
                ("parent_state", "!=", "cancel"),
            ]
        )
        from_clause = query.from_clause
        where_clause = query.where_clause

        aml = SQL.identifier("account_move_line")

        select_mapping = SQL(", ").join(SQL(v) for v in self._sql_mapping.values())

        full_query = SQL(
            """
            WITH amount AS (
                SELECT
                    %(aml)s.account_id,
                    CASE WHEN acc.account_type IN ('liability_payable', 'asset_receivable')
                        THEN %(aml)s.partner_id
                        ELSE NULL
                    END AS partner_id,
                    %(aml)s.currency_id,
                    %(aml)s.debit,
                    %(aml)s.credit,
                    %(aml)s.amount_currency,
                    %(aml)s.id as origin_aml_id
                FROM %(from_clause)s
                LEFT JOIN account_move am ON %(aml)s.move_id = am.id
                INNER JOIN account_account acc ON %(aml)s.account_id = acc.id
                LEFT JOIN account_partial_reconcile aprc
                    ON (%(aml)s.balance < 0 AND %(aml)s.id = aprc.credit_move_id)
                LEFT JOIN account_move_line amlcf
                    ON (
                        %(aml)s.balance < 0
                        AND aprc.debit_move_id = amlcf.id
                        AND amlcf.date < %(reval_date)s
                    )
                LEFT JOIN account_partial_reconcile aprd
                    ON (%(aml)s.balance > 0 AND %(aml)s.id = aprd.debit_move_id)
                LEFT JOIN account_move_line amldf
                    ON (
                        %(aml)s.balance > 0
                        AND aprd.credit_move_id = amldf.id
                        AND amldf.date < %(reval_date)s
                    )
                WHERE
                    %(aml)s.account_id IN %(account_ids)s
                    AND %(aml)s.date <= %(reval_date)s
                    %(date_filter)s
                    AND %(aml)s.currency_id IS NOT NULL
                    AND am.state = 'posted'
                    AND %(aml)s.balance <> 0
                    AND %(where_clause)s
                GROUP BY
                    acc.account_type,
                    origin_aml_id
                HAVING
                    %(aml)s.amount_residual_currency <> 0
            )
            SELECT
                account_id as id,
                origin_aml_id,
                partner_id,
                currency_id,
                %(select_mapping)s
            FROM amount
            GROUP BY
                account_id,
                origin_aml_id,
                currency_id,
                partner_id
            ORDER BY account_id, partner_id, currency_id
            """,
            aml=aml,
            from_clause=from_clause,
            where_clause=where_clause,
            reval_date=revaluation_date,
            account_ids=tuple(self.ids),
            date_filter=SQL("AND %s.date >= %s", aml, start_date)
            if start_date
            else SQL(""),
            select_mapping=select_mapping,
        )

        return full_query

    def compute_revaluations(self, revaluation_date, start_date=None):
        full_query = self._revaluation_query(revaluation_date, start_date)
        self.env.cr.execute(full_query)
        lines = self.env.cr.dictfetchall()

        data = {}
        for line in lines:
            account_id, currency_id, partner_id, origin_aml_id = (
                line["id"],
                line["currency_id"],
                line["partner_id"],
                line["origin_aml_id"],
            )
            data.setdefault(account_id, {})
            data[account_id].setdefault(partner_id, {})
            data[account_id][partner_id].setdefault(currency_id, {})
            # If partially reconciled, we need to adjust the balance according to
            # the partially reconciled items on the current line.
            origin_aml = self.env["account.move.line"].browse(origin_aml_id)
            if origin_aml.matched_debit_ids | origin_aml.matched_credit_ids:
                debit_move_ids = origin_aml.matched_debit_ids.mapped("debit_move_id")
                credit_move_ids = origin_aml.matched_credit_ids.mapped("credit_move_id")
                total_debit = line["debit"] + sum(debit_move_ids.mapped("debit"))
                total_credit = line["credit"] + sum(credit_move_ids.mapped("credit"))
                total_balance = total_debit - total_credit
                total_balance_currency = (
                    line["foreign_balance"]
                    + sum(debit_move_ids.mapped("amount_currency"))
                    + sum(credit_move_ids.mapped("amount_currency"))
                )
                line.update(
                    {
                        "debit": round(total_debit, 2),
                        "credit": round(total_credit, 2),
                        "balance": round(total_balance, 2),
                        "foreign_balance": round(total_balance_currency, 2),
                    }
                )
            existing_line = data[account_id][partner_id][currency_id]
            if existing_line:
                data[account_id][partner_id][
                    currency_id
                ] = self._merge_currency_revaluation_lines(existing_line, line)
            else:
                # Convert origin account move lines to list as there can be multiple
                line["origin_aml_id"] = [line["origin_aml_id"]]
                data[account_id][partner_id][currency_id] = line
        return data

    @api.model
    def _merge_currency_revaluation_lines(self, first_line, second_line):
        resulting_line = first_line
        resulting_line["origin_aml_id"].append(second_line["origin_aml_id"])
        resulting_line["balance"] += second_line["balance"]
        resulting_line["debit"] += second_line["debit"]
        resulting_line["credit"] += second_line["credit"]
        resulting_line["foreign_balance"] += second_line["foreign_balance"]
        return resulting_line
