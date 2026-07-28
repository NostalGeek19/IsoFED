import math
import pygame


class Light:
    __slots__ = ('tile_x', 'tile_y', 'color', 'radius', 'intensity')

    def __init__(self, tile_x, tile_y, color=(255, 180, 90), radius=3.5, intensity=1.0):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.color = color          
        self.radius = radius        
        self.intensity = intensity  


class LightingSystem:

    def __init__(self):
        self.lights = {}   # (tile_x, tile_y) -> Light

    def toggle_light_at(self, tile_x, tile_y, color=(255, 180, 90), radius=3.5, intensity=1.0):
        key = (int(tile_x), int(tile_y))
        if key in self.lights:
            del self.lights[key]
            return False
        self.lights[key] = Light(key[0], key[1], color, radius, intensity)
        return True

    def has_light_at(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.lights

    def remove_all(self):
        self.lights.clear()

    def get_lights(self):
        return list(self.lights.values())

    def count(self):
        return len(self.lights)

    def get_tile_light_boost(self, world_x, world_y):
        if not self.lights:
            return (0, 0, 0)

        total_r = total_g = total_b = 0.0
        for light in self.lights.values():
            dx = world_x - light.tile_x
            dy = world_y - light.tile_y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist >= light.radius:
                continue

            falloff = (1.0 - dist / light.radius) ** 2
            strength = falloff * light.intensity * 90.0
            total_r += light.color[0] / 255.0 * strength
            total_g += light.color[1] / 255.0 * strength
            total_b += light.color[2] / 255.0 * strength

        return (total_r, total_g, total_b)

    def render(self, screen, world_to_screen_fn, pixels_per_tile, clip_rect=None, chunk_bounds=None):
        return

    def render_placement_markers(self, screen, world_to_screen_fn, pixels_per_tile):
        if not self.lights:
            return
        screen_w, screen_h = screen.get_size()
        for light in self.lights.values():
            screen_x, screen_y = world_to_screen_fn(light.tile_x, light.tile_y)
            if -20 <= screen_x <= screen_w + 20 and -20 <= screen_y <= screen_h + 20:
                r = max(2, int(pixels_per_tile * 0.06))
                pygame.draw.circle(screen, (255, 215, 0), (int(screen_x), int(screen_y)), r, 1)
