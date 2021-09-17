from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockMove(models.Model):
    _inherit = "stock.move"

    @api.multi
    def action_confirm(self):
        """ Confirms stock move or put it in waiting if it's linked to another move. """
        move_create_proc = self.env['stock.move']
        move_to_confirm = self.env['stock.move']
        move_waiting = self.env['stock.move']

        to_assign = {}
        self.set_default_price_unit_from_product()
        for move in self:
            # if the move is preceeded, then it's waiting (if preceeding move is done, then action_assign has been called already and its state is already available)
            if move.move_orig_ids:
                move_waiting |= move
            # if the move is split and some of the ancestor was preceeded, then it's waiting as well
            else:
                inner_move = move.split_from
                while inner_move:
                    if inner_move.move_orig_ids:
                        move_waiting |= move
                        break
                    inner_move = inner_move.split_from
                else:
                    if move.procure_method == 'make_to_order':
                        move_create_proc |= move
                    else:
                        move_to_confirm |= move

            if not move.picking_id and move.picking_type_id:
                key = (move.group_id.id, move.location_id.id, move.location_dest_id.id)
                if key not in to_assign:
                    to_assign[key] = self.env['stock.move']
                to_assign[key] |= move
                print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
                move_to_confirm.write({'state': 'draft'})
            else:
                print("confirmeddddddddd")
                move_to_confirm.write({'state': 'confirmed'})


        # create procurements for make to order moves
        procurements = self.env['procurement.order']
        for move in move_create_proc:
            procurements |= procurements.create(move._prepare_procurement_from_move())
        if procurements:
            procurements.run()
        (move_waiting | move_create_proc).write({'state': 'waiting'})

        # assign picking in batch for all confirmed move that share the same details
        for key, moves in to_assign.items():
            moves.assign_picking()
        self._push_apply()
        return self

class StockPicking(models.Model):
    _inherit = "stock.picking"
    @api.multi
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        self.filtered(lambda picking: picking.state == 'draft').action_confirm()
        moves = self.mapped('move_lines').filtered(lambda move: move.state not in ('cancel', 'done'))
        if not moves:
            raise UserError(_('Nothing to check the availability for.'))
        moves.action_assign()
        return True
