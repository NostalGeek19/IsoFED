DIG_DARKEN_STRENGTH = 0.45   


class DugTile:
    __slots__ = ('tile_x', 'tile_y', 'water_level')

    def __init__(self, tile_x, tile_y):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.water_level = 0.0  


class DiggingSystem:

    def __init__(self, darken_strength=DIG_DARKEN_STRENGTH):
        self.darken_strength = darken_strength
        self.holes = {}   # (tile_x, tile_y) -> DugTile

    # ------------------------------------------------------------------
    def dig_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        if key in self.holes:
            return False
        self.holes[key] = DugTile(key[0], key[1])
        return True

    def fill_dirt_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        return self.holes.pop(key, None) is not None

    def toggle_dig_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        if key in self.holes:
            del self.holes[key]
            return False
        self.holes[key] = DugTile(key[0], key[1])
        return True

    def is_dug(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.holes

    def get_hole(self, tile_x, tile_y):
        return self.holes.get((int(tile_x), int(tile_y)))

    def get_water_level(self, tile_x, tile_y):
        hole = self.holes.get((int(tile_x), int(tile_y)))
        return hole.water_level if hole is not None else 0.0

    def set_water_level(self, tile_x, tile_y, level):
        hole = self.holes.get((int(tile_x), int(tile_y)))
        if hole is not None:
            hole.water_level = max(0.0, min(1.0, level))

    def count(self):
        return len(self.holes)

    def remove_all(self):
        self.holes.clear()

    # ------------------------------------------------------------------
    def get_darken_factor(self, tile_x, tile_y):
        hole = self.holes.get((int(tile_x), int(tile_y)))
        if hole is None:
            return 0.0
        return self.darken_strength * (1.0 - hole.water_level)

    def apply_darken(self, tile_x, tile_y, color):
        darken = self.get_darken_factor(tile_x, tile_y)
        if darken <= 0.0:
            return color
        mult = 1.0 - darken
        return tuple(max(0, int(c * mult)) for c in color)

    # ------------------------------------------------------------------
    def get_status_text(self):
        if not self.holes:
            return "no holes"
        return f"{len(self.holes)} hole(s)"
