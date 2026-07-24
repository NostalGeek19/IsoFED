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
        self._glow_cache = {}

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

    def _get_glow_surface(self, color, size):
        key = (color, size)
        cached = self._glow_cache.get(key)
        if cached is not None:
            return cached

        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        center = size / 2
        steps = 36
        for i in range(steps, 0, -1):
            t = i / steps
            r = int(center * t)

            alpha = int(70 * (1.0 - t) ** 3)
            if alpha <= 0:
                continue
            pygame.draw.circle(surf, (*color, alpha), (int(center), int(center)), r)

        if len(self._glow_cache) > 40:
            self._glow_cache.clear()
        self._glow_cache[key] = surf
        return surf

    def render(self, screen, world_to_screen_fn, pixels_per_tile, clip_rect=None, chunk_bounds=None):
        if not self.lights:
            return

        screen_w, screen_h = screen.get_size()
        previous_clip = screen.get_clip()
        if clip_rect is not None:
            screen.set_clip(clip_rect)

        for light in self.lights.values():
            if chunk_bounds is not None:
                min_x, min_y, max_x, max_y = chunk_bounds
                if not (min_x <= light.tile_x < max_x and min_y <= light.tile_y < max_y):
                    continue
            
            screen_x, screen_y = world_to_screen_fn(light.tile_x, light.tile_y)

            radius_px = max(1.0, light.radius * pixels_per_tile)
            if (screen_x + radius_px < 0 or screen_x - radius_px > screen_w or
                    screen_y + radius_px < 0 or screen_y - radius_px > screen_h):
                continue

            # Lamppost: a slender dark stem + a bright "bulb"
            post_height = pixels_per_tile * 0.9
            post_top = (screen_x, screen_y - post_height)
            pygame.draw.line(screen, (40, 35, 30), (screen_x, screen_y), post_top, max(1, int(pixels_per_tile * 0.06)))

            # he glow of the bulb itself is faint, extending only immediately around it.
            bulb_radius = max(2, int(pixels_per_tile * 0.12))
            bulb_glow_size = bulb_radius * 4
            bulb_glow = self._get_glow_surface((255, 250, 230), bulb_glow_size)
            bulb_glow_rect = bulb_glow.get_rect(center=(int(post_top[0]), int(post_top[1])))
            screen.blit(bulb_glow, bulb_glow_rect, special_flags=pygame.BLEND_RGBA_ADD)
            pygame.draw.circle(screen, (255, 250, 230), (int(post_top[0]), int(post_top[1])), bulb_radius)

        screen.set_clip(previous_clip)

    def render_placement_markers(self, screen, world_to_screen_fn, pixels_per_tile):
        if not self.lights:
            return
        screen_w, screen_h = screen.get_size()
        for light in self.lights.values():
            screen_x, screen_y = world_to_screen_fn(light.tile_x, light.tile_y)
            if -20 <= screen_x <= screen_w + 20 and -20 <= screen_y <= screen_h + 20:
                r = max(2, int(pixels_per_tile * 0.06))
                pygame.draw.circle(screen, (255, 215, 0), (int(screen_x), int(screen_y)), r, 1)