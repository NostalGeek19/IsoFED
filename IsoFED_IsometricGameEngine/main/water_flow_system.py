import math


FILL_SPEED = 1.0 / 4.0            
WATER_COLOR = (64, 164, 223)      
WATER_TINT_STRENGTH = 0.9         
CHAIN_THRESHOLD = 0.5             


NEIGHBOR_SPEEDUP_PER_SOURCE = 0.35
MAX_NEIGHBOR_SPEEDUP = 3

WIDE_POOL_SPEED_MULT = 0.75       
CORRIDOR_SPEED_MULT = 2.3         


MAX_SPEED_MULTIPLIER = 5.0


def _tile_speed_variance(tile_x, tile_y):
    h = math.sin(tile_x * 12.9898 + tile_y * 78.233) * 43758.5453
    frac = h - math.floor(h)
    return 0.8 + 0.4 * frac


class WaterFlowSystem:

    def __init__(self, digging_system, fill_speed=FILL_SPEED,
                 water_color=WATER_COLOR, tint_strength=WATER_TINT_STRENGTH,
                 chain_threshold=CHAIN_THRESHOLD):
        self.digging = digging_system
        self.fill_speed = fill_speed
        self.water_color = water_color
        self.tint_strength = tint_strength
        self.chain_threshold = chain_threshold

    # ------------------------------------------------------------------
    def set_fill_speed(self, fill_speed):
        self.fill_speed = max(0.0001, fill_speed)

    def get_fill_speed(self):
        return self.fill_speed

    # ------------------------------------------------------------------
    def update(self, dt, water_neighbor_fn):
        holes = self.digging.holes
        if not holes:
            return

        to_fill = []
        for key, hole in holes.items():
            if hole.water_level >= 1.0:
                continue
            source_count = self._count_water_neighbors(key, holes, water_neighbor_fn)
            if source_count > 0:
                to_fill.append((hole, source_count))

        for hole, source_count in to_fill:
            speedup = 1.0 + NEIGHBOR_SPEEDUP_PER_SOURCE * min(source_count - 1, MAX_NEIGHBOR_SPEEDUP)
            speedup *= self._shape_speed_multiplier((hole.tile_x, hole.tile_y), holes)
            speedup = min(speedup, MAX_SPEED_MULTIPLIER)
            speedup *= _tile_speed_variance(hole.tile_x, hole.tile_y)
            hole.water_level = min(1.0, hole.water_level + self.fill_speed * speedup * dt)

    def _shape_speed_multiplier(self, key, holes):
        x, y = key

        
        for ox, oy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
            corners = ((x + ox, y + oy), (x + ox + 1, y + oy),
                       (x + ox, y + oy + 1), (x + ox + 1, y + oy + 1))
            if all(c in holes for c in corners):
                return WIDE_POOL_SPEED_MULT

        dug_neighbor_count = sum(
            1 for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if (x + dx, y + dy) in holes
        )
        if dug_neighbor_count <= 2:
            return CORRIDOR_SPEED_MULT

        return 1.0

    def _count_water_neighbors(self, key, holes, water_neighbor_fn):
        x, y = key
        count = 0
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor_key = (x + dx, y + dy)
            neighbor_hole = holes.get(neighbor_key)
            if neighbor_hole is not None:
                if neighbor_hole.water_level >= self.chain_threshold:
                    count += 1
                continue
            if water_neighbor_fn is not None and water_neighbor_fn(neighbor_key[0], neighbor_key[1]):
                count += 1
        return count

    # ------------------------------------------------------------------
    def get_water_color(self, tile_x, tile_y):
        hole = self.digging.holes.get((int(tile_x), int(tile_y)))
        if hole is None or hole.water_level <= 0.0:
            return None
        eased = hole.water_level ** 0.7
        return self.water_color, eased * self.tint_strength

    def is_full(self, tile_x, tile_y):
        hole = self.digging.holes.get((int(tile_x), int(tile_y)))
        return hole is not None and hole.water_level >= 1.0

    # ------------------------------------------------------------------
    def get_status_text(self):
        holes = self.digging.holes
        if not holes:
            return "no water"
        filling = sum(1 for h in holes.values() if 0.0 < h.water_level < 1.0)
        filled = sum(1 for h in holes.values() if h.water_level >= 1.0)
        return f"{filling} filling, {filled} filled"