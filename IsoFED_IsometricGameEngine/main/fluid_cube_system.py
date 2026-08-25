import random

SPREAD_INTERVAL = 0.6            
SPREAD_SHARE = 0.35              
MIN_SOURCE_FILL_TO_SPREAD = 0.08
MIN_RETAIN_FILL = 0.02          

OPEN_LIFETIME_RANGE = (10.0, 15.0)   
REACTION_PRODUCT = 'stone_cube'

MAX_CONTAINMENT_REGION = 400     

_NEIGHBOR_OFFSETS = ((1, 0), (-1, 0), (0, 1), (0, -1))

_KIND_TO_TYPE = {'water': 'water_cube', 'lava': 'lava_cube'}


class FluidCubeSystem:

    def __init__(self, seed=None, on_tile_changed=None):
        self.rng = random.Random(seed)
        # (tile_x, tile_y, level) -> dict(kind, age, spread_timer, contained, evaporate_at)
        self.tiles = {}
        
        self.on_tile_changed = on_tile_changed

    def _notify(self, tile_x, tile_y):
        if self.on_tile_changed is not None:
            self.on_tile_changed(tile_x, tile_y)

    # ------------------------------------------------------------------
    def pour(self, objects, tile_x, tile_y, kind):
        if kind not in _KIND_TO_TYPE:
            return False
        tile_x, tile_y = int(tile_x), int(tile_y)

        top_type = objects.get_top_object_type(tile_x, tile_y)
        top_fluid_kind = objects.get_fluid_kind(top_type) if top_type else None

        if top_fluid_kind is not None:
            if top_fluid_kind != kind:
                return False
            top_level = objects.get_stack_height(tile_x, tile_y) - 1
            existing = self.tiles.get((tile_x, tile_y, top_level))
            if existing is not None:
                objects.set_fill(tile_x, tile_y, top_level, 1.0)
                existing['open_timer'] = 0.0
                existing['contained'] = False
                existing['evaporate_at'] = None
                return True


        if objects.is_stack_full(tile_x, tile_y):
            return False

        obj_type = _KIND_TO_TYPE[kind]
        level = objects.get_stack_height(tile_x, tile_y)
        if not objects.place_object_at(tile_x, tile_y, obj_type=obj_type, mirrored=False, level=level):
            return False

        objects.set_fill(tile_x, tile_y, level, 1.0)
        self.tiles[(tile_x, tile_y, level)] = {
            'kind': kind, 'open_timer': 0.0, 'spread_timer': 0.0,
            'contained': False, 'evaporate_at': None,
        }
        self._notify(tile_x, tile_y)
        return True

    def is_fluid_tile(self, tile_x, tile_y, level=None):
        if level is not None:
            return (int(tile_x), int(tile_y), int(level)) in self.tiles
        tx, ty = int(tile_x), int(tile_y)
        return any(t[0] == tx and t[1] == ty for t in self.tiles)

    def count(self):
        return len(self.tiles)

    # ------------------------------------------------------------------
    def update(self, dt, objects):
        if not self.tiles:
            return

        self._resolve_reactions(objects)
        if not self.tiles:
            return

        checked_regions = set()
        to_evaporate = []

        for cell, state in list(self.tiles.items()):
            if cell not in self.tiles:
                continue   # consumed by a reaction above

            if cell not in checked_regions:
                contained, region = self._check_containment(objects, cell)
                for region_cell in region:
                    checked_regions.add(region_cell)
                    region_state = self.tiles.get(region_cell)
                    if region_state is None:
                        continue
                    was_contained = region_state['contained']
                    region_state['contained'] = contained
                    if contained:
                        region_state['open_timer'] = 0.0
                        region_state['evaporate_at'] = None
                    elif was_contained or region_state['evaporate_at'] is None:
                        region_state['evaporate_at'] = self.rng.uniform(*OPEN_LIFETIME_RANGE)
                        region_state['open_timer'] = 0.0

            if not state['contained']:
                state['open_timer'] += dt
                if state['open_timer'] >= (state['evaporate_at'] or OPEN_LIFETIME_RANGE[0]):
                    to_evaporate.append(cell)
                    continue

            state['spread_timer'] += dt
            if state['spread_timer'] >= SPREAD_INTERVAL:
                state['spread_timer'] = 0.0
                self._try_spread(objects, cell, state)

        for cell in to_evaporate:
            self._remove_fluid(objects, cell)

    # ------------------------------------------------------------------
    def _check_containment(self, objects, start):
        visited = {start}
        frontier = [start]
        region = []

        while frontier:
            current = frontier.pop()
            region.append(current)
            if len(region) > MAX_CONTAINMENT_REGION:
                return False, region

            cx, cy, level = current
            for dx, dy in _NEIGHBOR_OFFSETS:
                neighbor = (cx + dx, cy + dy, level)
                if neighbor in visited:
                    continue
                if neighbor in self.tiles:
                    visited.add(neighbor)
                    frontier.append(neighbor)
                    continue
                if not self._is_solid_wall(objects, neighbor[0], neighbor[1], level):
                    return False, region

        return True, region

    @staticmethod
    def _is_solid_wall(objects, tile_x, tile_y, level):
        entry = objects.get_object_at_level(tile_x, tile_y, level)
        if entry is None:
            return False
        obj_type, _mirrored = entry
        return objects.get_fluid_kind(obj_type) is None

    # ------------------------------------------------------------------
    def _try_spread(self, objects, cell, state):
        tx, ty, level = cell
        fill = objects.get_fill(tx, ty, level)
        if fill <= MIN_SOURCE_FILL_TO_SPREAD:
            return

        kind = state['kind']
        obj_type = _KIND_TO_TYPE[kind]

        targets = []   # ((nx, ny, level), already_exists)
        for dx, dy in _NEIGHBOR_OFFSETS:
            nx, ny = tx + dx, ty + dy
            neighbor_height = objects.get_stack_height(nx, ny)

            top_type = objects.get_top_object_type(nx, ny)
            top_fluid_kind = objects.get_fluid_kind(top_type) if top_type else None

            if top_fluid_kind == kind:
                neighbor_level = neighbor_height - 1
                if neighbor_level > level:
                    continue   # that puddle is genuinely higher up than us
                neighbor_cell = (nx, ny, neighbor_level)
                if neighbor_cell in self.tiles and objects.get_fill(nx, ny, neighbor_level) < fill - 0.05:
                    targets.append((neighbor_cell, True))
            elif top_fluid_kind is None:
                if neighbor_height > level:
                    continue   

                if not objects.is_stack_full(nx, ny):
                    targets.append(((nx, ny, neighbor_height), False))
            
        if not targets:
            return

        share = min(fill * SPREAD_SHARE, fill - MIN_RETAIN_FILL)
        if share <= 0:
            return
        share_each = share / len(targets)

        for (nx, ny, nlevel), already_exists in targets:
            if not already_exists:
                if not objects.place_object_at(nx, ny, obj_type=obj_type, mirrored=False, level=nlevel):
                    continue
                objects.set_fill(nx, ny, nlevel, share_each)

                self.tiles[(nx, ny, nlevel)] = {
                    'kind': kind, 'open_timer': state['open_timer'], 'spread_timer': 0.0,
                    'contained': False, 'evaporate_at': state['evaporate_at'],
                }
                self._notify(nx, ny)
            else:
                current = objects.get_fill(nx, ny, nlevel)
                objects.set_fill(nx, ny, nlevel, current + share_each)

        fill -= share
        objects.set_fill(tx, ty, level, fill)

    # ------------------------------------------------------------------
    def _remove_fluid(self, objects, cell):
        state = self.tiles.pop(cell, None)
        if state is None:
            return
        tx, ty, level = cell
        obj_type = _KIND_TO_TYPE[state['kind']]
        if objects.get_top_object_type(tx, ty) == obj_type and objects.get_stack_height(tx, ty) - 1 == level:
            objects.remove_top_object_at(tx, ty)
        objects.set_fill(tx, ty, level, 1.0)
        self._notify(tx, ty)

    def _resolve_reactions(self, objects):
        water_cells = {c for c, s in self.tiles.items() if s['kind'] == 'water'}
        lava_cells = {c for c, s in self.tiles.items() if s['kind'] == 'lava'}
        if not water_cells or not lava_cells:
            return

        to_convert = set()
        for cell in lava_cells:
            tx, ty, level = cell
            for dx, dy in _NEIGHBOR_OFFSETS:
                neighbor = (tx + dx, ty + dy, level)
                if neighbor in water_cells:
                    to_convert.add(cell)
                    to_convert.add(neighbor)

        for cell in to_convert:
            state = self.tiles.pop(cell, None)
            if state is None:
                continue
            tx, ty, level = cell
            obj_type = _KIND_TO_TYPE[state['kind']]
            if objects.get_top_object_type(tx, ty) == obj_type and objects.get_stack_height(tx, ty) - 1 == level:
                objects.remove_top_object_at(tx, ty)
                objects.set_fill(tx, ty, level, 1.0)
                objects.place_object_at(tx, ty, obj_type=REACTION_PRODUCT, mirrored=False, level=level)
                self._notify(tx, ty)

    # ------------------------------------------------------------------
    def get_status_text(self):
        if not self.tiles:
            return "no fluids"
        water = sum(1 for s in self.tiles.values() if s['kind'] == 'water')
        lava = len(self.tiles) - water
        contained = sum(1 for s in self.tiles.values() if s['contained'])
        return f"water {water}, lava {lava} ({contained} contained)"