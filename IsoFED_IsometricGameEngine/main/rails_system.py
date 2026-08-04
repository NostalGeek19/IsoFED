RAIL_TYPE = 'rail'
CART_TYPE = 'cart'

AXIS_EW = 'ew'  
AXIS_NS = 'ns'   

DEFAULT_CART_SPEED = 2.2  


# ----------------------------------------------------------------------
def get_rail_axis(mirrored):
    return AXIS_NS if mirrored else AXIS_EW


def needs_camera_flip(obj_type, camera_rotation_steps):
    if obj_type not in (RAIL_TYPE, CART_TYPE):
        return False
    return int(camera_rotation_steps) % 2 == 1


# ----------------------------------------------------------------------
def can_place_rail(objects, tile_x, tile_y, is_blocked_fn=None):
    if objects.has_object_at(tile_x, tile_y):
        return False
    if is_blocked_fn is not None and is_blocked_fn(tile_x, tile_y):
        return False
    return True


def can_place_cart(objects, tile_x, tile_y):
    return (objects.get_stack_height(tile_x, tile_y) == 1 and
            objects.get_top_object_type(tile_x, tile_y) == RAIL_TYPE)


def get_rail_mirrored_at(objects, tile_x, tile_y):
    for level, obj_type, mirrored in objects.get_stack_with_levels(tile_x, tile_y):
        if obj_type == RAIL_TYPE:
            return mirrored
    return None


_NEIGHBOR_OFFSETS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def find_connected_rail_directions(objects, tile_x, tile_y):
    result = []
    for dx, dy in _NEIGHBOR_OFFSETS:
        if can_place_cart(objects, tile_x + dx, tile_y + dy):
            result.append((dx, dy))
    return result


def choose_direction(objects, tile_x, tile_y, want_positive, avoid=None):
    candidates = find_connected_rail_directions(objects, tile_x, tile_y)
    if not candidates:
        return None
    if avoid is not None and len(candidates) > 1:
        filtered = [c for c in candidates if c != avoid]
        if filtered:
            candidates = filtered

    if want_positive:
        preferred = [c for c in candidates if c[0] > 0 or (c[0] == 0 and c[1] > 0)]
    else:
        preferred = [c for c in candidates if c[0] < 0 or (c[0] == 0 and c[1] < 0)]
    return preferred[0] if preferred else candidates[0]


# ----------------------------------------------------------------------
class RailCart:
    __slots__ = ('tile_x', 'tile_y', 'move_offset', 'moving', 'progress',
                 'from_tile', 'to_tile')

    def __init__(self, tile_x, tile_y):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.move_offset = None   # (dx, dy)
        self.moving = False
        self.progress = 0.0      
        self.from_tile = (tile_x, tile_y)
        self.to_tile = (tile_x, tile_y)

    def get_render_position(self):
        if self.progress <= 0.0:
            return float(self.tile_x), float(self.tile_y)
        fx, fy = self.from_tile
        tx, ty = self.to_tile
        t = min(1.0, self.progress)
        return fx + (tx - fx) * t, fy + (ty - fy) * t


class RailsSystem:
    def __init__(self, move_speed=DEFAULT_CART_SPEED):
        self.move_speed = move_speed
        self.cart = None   # RailCart

    # ------------------------------------------------------------------
    def is_locked(self):
        return self.cart is not None

    def locked_tile(self):
        return (self.cart.tile_x, self.cart.tile_y) if self.cart is not None else None

    def toggle_lock(self, objects, tile_x, tile_y):
        tile_x, tile_y = int(tile_x), int(tile_y)

        if self.cart is not None:
            self.cart = None
            return False

        if objects.get_top_object_type(tile_x, tile_y) != CART_TYPE:
            return False

        self.cart = RailCart(tile_x, tile_y)
        return True

    def set_direction(self, direction, objects):
        if self.cart is None:
            return None
        avoid = None
        if self.cart.move_offset is not None:
            avoid = (-self.cart.move_offset[0], -self.cart.move_offset[1])
        chosen = choose_direction(objects, self.cart.tile_x, self.cart.tile_y,
                                   want_positive=(direction > 0), avoid=avoid)
        if chosen is None:
            return None
        self.cart.move_offset = chosen
        self.cart.moving = True
        return chosen

    def stop(self):
        if self.cart is not None:
            self.cart.moving = False
            self.cart.move_offset = None

    def get_camera_target_tile(self):
        if self.cart is None:
            return None
        return self.cart.get_render_position()

    # ------------------------------------------------------------------
    def update(self, dt, objects):
        if self.cart is None:
            return

        cart = self.cart

        if cart.progress <= 0.0:
            if objects.get_top_object_type(cart.tile_x, cart.tile_y) != CART_TYPE:
                self.cart = None
                return
        else:
            from_x, from_y = cart.from_tile
            if objects.get_top_object_type(from_x, from_y) != CART_TYPE:
                self.cart = None
                return
            to_x, to_y = cart.to_tile
            if not can_place_cart(objects, to_x, to_y):
                cart.moving = False
                cart.progress = 0.0
                return

        if not cart.moving:
            return

        if cart.progress <= 0.0:
            if cart.move_offset is None:
                cart.moving = False
                return

            dx, dy = cart.move_offset
            next_tile = (cart.tile_x + dx, cart.tile_y + dy)

            if not can_place_cart(objects, next_tile[0], next_tile[1]):
                cart.moving = False
                return

            cart.from_tile = (cart.tile_x, cart.tile_y)
            cart.to_tile = next_tile

        cart.progress += self.move_speed * dt
        if cart.progress >= 1.0:
            self._finish_move(objects)

    def _finish_move(self, objects):
        cart = self.cart
        from_x, from_y = cart.from_tile
        to_x, to_y = cart.to_tile

        rail_mirrored = get_rail_mirrored_at(objects, to_x, to_y)
        objects.remove_top_object_at(from_x, from_y)
        objects.place_object_at(to_x, to_y, CART_TYPE, mirrored=rail_mirrored)

        cart.tile_x, cart.tile_y = to_x, to_y
        cart.progress = 0.0
