from odoo import api, fields, models, _
from odoo.tools.float_utils import float_compare

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
            # elif move.split_from.backorder_id:
            #     print("backorder_idddddddd")
            #     move_to_confirm.write({'state': 'draft'})
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
    def do_transfer(self):
        """ If no pack operation, we do simple action_done of the picking.
        Otherwise, do the pack operations. """
        # TDE CLEAN ME: reclean me, please
        self._create_lots_for_picking()

        no_pack_op_pickings = self.filtered(lambda picking: not picking.pack_operation_ids)
        no_pack_op_pickings.action_done()
        other_pickings = self - no_pack_op_pickings
        for picking in other_pickings:
            need_rereserve, all_op_processed = picking.picking_recompute_remaining_quantities()
            todo_moves = self.env['stock.move']
            toassign_moves = self.env['stock.move']

            # create extra moves in the picking (unexpected product moves coming from pack operations)
            if not all_op_processed:
                todo_moves |= picking._create_extra_moves()

            if need_rereserve or not all_op_processed:
                moves_reassign = any(x.origin_returned_move_id or x.move_orig_ids for x in picking.move_lines if
                                     x.state not in ['done', 'cancel'])
                if moves_reassign and picking.location_id.usage not in ("supplier", "production", "inventory"):
                    # unnecessary to assign other quants than those involved with pack operations as they will be unreserved anyways.
                    picking.with_context(reserve_only_ops=True, no_state_change=True).rereserve_quants(
                        move_ids=picking.move_lines.ids)
                picking.do_recompute_remaining_quantities()

            # split move lines if needed
            for move in picking.move_lines:
                rounding = move.product_id.uom_id.rounding
                remaining_qty = move.remaining_qty
                if move.state in ('done', 'cancel'):
                    # ignore stock moves cancelled or already done
                    continue
                elif move.state == 'draft':
                    toassign_moves |= move
                if float_compare(remaining_qty, 0, precision_rounding=rounding) == 0:
                    if move.state in ('draft', 'assigned', 'confirmed'):
                        todo_moves |= move
                elif float_compare(remaining_qty, 0, precision_rounding=rounding) > 0 and float_compare(remaining_qty,
                                                                                                        move.product_qty,
                                                                                                        precision_rounding=rounding) < 0:
                    # TDE FIXME: shoudl probably return a move - check for no track key, by the way
                    new_move_id = move.split(remaining_qty)
                    new_move = self.env['stock.move'].with_context(mail_notrack=True).browse(new_move_id)
                    todo_moves |= move
                    # Assign move as it was assigned before
                    toassign_moves |= new_move

            # TDE FIXME: do_only_split does not seem used anymore
            if todo_moves and not self.env.context.get('do_only_split'):
                todo_moves.action_done()
            elif self.env.context.get('do_only_split'):
                picking = picking.with_context(split=todo_moves.ids)

            picking._create_backorder()
        return True

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




class Quant(models.Model):
    """ Quants are the smallest unit of stock physical instances """
    _inherit = "stock.quant"
    @api.model
    def quants_reserve(self, quants, move, link=False):
        ''' This function reserves quants for the given move and optionally
        given link. If the total of quantity reserved is enough, the move state
        is also set to 'assigned'

        :param quants: list of tuple(quant browse record or None, qty to reserve). If None is given as first tuple element, the item will be ignored. Negative quants should not be received as argument
        :param move: browse record
        :param link: browse record (stock.move.operation.link)
        '''
        # TDE CLEANME: use ids + quantities dict
        # TDE CLEANME: check use of sudo
        quants_to_reserve_sudo = self.env['stock.quant'].sudo()
        reserved_availability = move.reserved_availability
        # split quants if needed
        for quant, qty in quants:
            if qty <= 0.0 or (quant and quant.qty <= 0.0):
                raise UserError(_('You can not reserve a negative quantity or a negative quant.'))
            if not quant:
                continue
            quant._quant_split(qty)
            quants_to_reserve_sudo |= quant
            reserved_availability += quant.qty
        # reserve quants
        if quants_to_reserve_sudo:
            quants_to_reserve_sudo.write({'reservation_id': move.id})
        # check if move state needs to be set as 'assigned'
        # TDE CLEANME: should be moved as a move model method IMO
        rounding = move.product_id.uom_id.rounding
        if float_compare(reserved_availability, move.product_qty, precision_rounding=rounding) == 0 and move.state in (
        'confirmed', 'waiting'):
            move.write({'state': 'draft'})
        elif float_compare(reserved_availability, 0, precision_rounding=rounding) > 0 and not move.partially_available:
            move.write({'partially_available': True})

class StockImmediateTransfer(models.TransientModel):
    _inherit = 'stock.immediate.transfer'
    _description = 'Immediate Transfer'

    # @api.multi
    # def process(self):
    #     self.ensure_one()
    #     # If still in draft => confirm and assign
    #     if self.pick_id.state == 'draft':
    #         self.pick_id.action_confirm()
    #         if self.pick_id.state != 'assigned':
    #             self.pick_id.action_assign()
    #             # if self.pick_id.state != 'assigned':
    #             #     raise UserError(_("Could not reserve all requested products. Please use the \'Mark as Todo\' button to handle the reservation manually."))
    #     for pack in self.pick_id.pack_operation_ids:
    #         if pack.product_qty > 0:
    #             pack.write({'qty_done': pack.product_qty})
    #         else:
    #             pack.unlink()
    #     return self.pick_id.do_transfer()
