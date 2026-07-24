import math
import pygame


class SunSystem:
    DAY_LENGTH_SECONDS = 180.0


    _KEYFRAMES = [
        (0.00, (60, 70, 110), 0.35),   
        (0.22, (90, 95, 130), 0.40),   
        (0.27, (255, 150, 90), 0.70),  
        (0.40, (255, 230, 180), 0.95), 
        (0.50, (255, 250, 235), 1.05), 
        (0.60, (255, 230, 180), 0.95), 
        (0.73, (255, 130, 80), 0.70),  
        (0.78, (90, 95, 130), 0.40),   
        (1.00, (60, 70, 110), 0.35),   
    ]


    TINT_BLEND = 0.16

    def __init__(self, screen_width, screen_height, start_time=0.5, day_length_seconds=None):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.time_of_day = start_time % 1.0
        self.day_length = day_length_seconds if day_length_seconds is not None else self.DAY_LENGTH_SECONDS
        self.paused = False

        self._glow_cache = {}


    def update(self, dt_seconds):
        if self.paused or self.day_length <= 0:
            return
        self.time_of_day = (self.time_of_day + dt_seconds / self.day_length) % 1.0

    def set_time(self, time_of_day):
        self.time_of_day = float(time_of_day) % 1.0

    def toggle_pause(self):
        self.paused = not self.paused

    def get_time_of_day(self):
        return self.time_of_day


    def get_elevation(self):
        return math.sin((self.time_of_day - 0.25) * 2 * math.pi)

    def is_daytime(self):
        return self.get_elevation() > 0.0

    def _azimuth_progress(self):
        return (self.time_of_day - 0.25) / 0.5

    def get_light_direction(self):
        progress = max(0.0, min(1.0, self._azimuth_progress()))
        angle = math.pi * (1.0 - progress)  # east (0) -> west (pi)
        return math.cos(angle), math.sin(angle)

    def _keyframe_color_and_brightness(self):
        t = self.time_of_day
        frames = self._KEYFRAMES
        for i in range(len(frames) - 1):
            t0, color0, b0 = frames[i]
            t1, color1, b1 = frames[i + 1]
            if t0 <= t <= t1:
                span = (t1 - t0) or 1e-6
                f = (t - t0) / span
                color = tuple(color0[c] + (color1[c] - color0[c]) * f for c in range(3))
                brightness = b0 + (b1 - b0) * f
                return color, brightness

        return frames[-1][1], frames[-1][2]

    def get_ambient_tint(self):
        return self._keyframe_color_and_brightness()

    def apply_tint(self, color):
        tint_color, brightness = self.get_ambient_tint()
        blended = []
        for i in range(3):
            c = color[i] * brightness
            c = c * (1 - self.TINT_BLEND) + tint_color[i] * self.TINT_BLEND
            blended.append(max(0, min(255, int(c))))
        return tuple(blended)


    def _get_sun_screen_position(self):
        progress = self._azimuth_progress()
        x = self.screen_width * progress
        elevation = self.get_elevation()
        y = self.screen_height * 0.5 - max(0.0, elevation) * self.screen_height * 0.42
        return x, y

    def _sun_color_and_radius(self):
        elevation = self.get_elevation()
        if elevation > 0.35:
            color = (255, 250, 225)
        elif elevation > 0.0:
            f = elevation / 0.35
            color = (255, int(250 * f + 90 * (1 - f)), int(225 * f + 60 * (1 - f)))
        else:
            color = (255, 90, 60)
        radius = 34 + 10 * max(0.0, elevation)
        return color, radius

    def _get_glow_surface(self, color, size):
        key = (tuple(color), size)
        cached = self._glow_cache.get(key)
        if cached is not None:
            return cached

        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        center = size / 2
        steps = 36
        for i in range(steps, 0, -1):
            t = i / steps
            r = int(center * t)
            alpha = int(200 * (1.0 - t) ** 2)
            if alpha <= 0:
                continue
            pygame.draw.circle(surf, (*color, alpha), (int(center), int(center)), r)

        if len(self._glow_cache) > 60:
            self._glow_cache.clear()
        self._glow_cache[key] = surf
        return surf

    def render_disc_and_glow(self, screen):
        elevation = self.get_elevation()
        if elevation <= -0.05:
            return None 

        visibility = max(0.0, min(1.0, (elevation + 0.05) / 0.2))
        sun_x, sun_y = self._get_sun_screen_position()
        color, radius = self._sun_color_and_radius()

        glow_size = int(radius * 6)
        glow = self._get_glow_surface(color, glow_size)
        if visibility < 1.0:
            glow = glow.copy()
            glow.set_alpha(int(255 * visibility))
        glow_rect = glow.get_rect(center=(int(sun_x), int(sun_y)))
        screen.blit(glow, glow_rect, special_flags=pygame.BLEND_RGBA_ADD)

        disk_surface = pygame.Surface((int(radius * 2), int(radius * 2)), pygame.SRCALPHA)
        disk_alpha = int(255 * visibility)
        pygame.draw.circle(disk_surface, (*color, disk_alpha), (int(radius), int(radius)), int(radius))
        screen.blit(disk_surface, disk_surface.get_rect(center=(int(sun_x), int(sun_y))))

        return sun_x, sun_y, color, radius, visibility

    def render_rays(self, screen, sun_params=None, clip_rect=None):
        if sun_params is None:
            elevation = self.get_elevation()
            if elevation <= -0.05:
                return
            visibility = max(0.0, min(1.0, (elevation + 0.05) / 0.2))
            sun_x, sun_y = self._get_sun_screen_position()
            color, radius = self._sun_color_and_radius()
        else:
            sun_x, sun_y, color, radius, visibility = sun_params

        if visibility <= 0.0:
            return

        previous_clip = screen.get_clip()
        if clip_rect is not None:
            screen.set_clip(clip_rect)

        self._render_rays(screen, sun_x, sun_y, color, radius, visibility)

        screen.set_clip(previous_clip)

    def render(self, screen):
        sun_params = self.render_disc_and_glow_without_disk(screen)
        if sun_params is not None:
            self.render_rays(screen, sun_params)
            self._draw_disk(screen, sun_params)

    def render_disc_and_glow_without_disk(self, screen):
        elevation = self.get_elevation()
        if elevation <= -0.05:
            return None
        visibility = max(0.0, min(1.0, (elevation + 0.05) / 0.2))
        sun_x, sun_y = self._get_sun_screen_position()
        color, radius = self._sun_color_and_radius()

        glow_size = int(radius * 6)
        glow = self._get_glow_surface(color, glow_size)
        if visibility < 1.0:
            glow = glow.copy()
            glow.set_alpha(int(255 * visibility))
        glow_rect = glow.get_rect(center=(int(sun_x), int(sun_y)))
        screen.blit(glow, glow_rect, special_flags=pygame.BLEND_RGBA_ADD)

        return sun_x, sun_y, color, radius, visibility

    def _draw_disk(self, screen, sun_params):
        sun_x, sun_y, color, radius, visibility = sun_params
        disk_surface = pygame.Surface((int(radius * 2), int(radius * 2)), pygame.SRCALPHA)
        disk_alpha = int(255 * visibility)
        pygame.draw.circle(disk_surface, (*color, disk_alpha), (int(radius), int(radius)), int(radius))
        screen.blit(disk_surface, disk_surface.get_rect(center=(int(sun_x), int(sun_y))))

    def _render_rays(self, screen, sun_x, sun_y, color, radius, visibility):
        num_rays = 10
        ray_length = self.screen_height * 1.3
        base_width = radius * 0.9

        drift = self.time_of_day * 40.0

        rays_surface = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        ray_color = (*color, int(26 * visibility))

        for i in range(num_rays):
            angle_deg = 90 + (i - num_rays / 2) * (140 / num_rays) + math.sin(drift + i) * 4
            angle = math.radians(angle_deg)
            dx, dy = math.cos(angle), math.sin(angle)
            perp_x, perp_y = -dy, dx

            tip_x = sun_x + dx * ray_length
            tip_y = sun_y + dy * ray_length

            p1 = (sun_x + perp_x * base_width, sun_y + perp_y * base_width)
            p2 = (sun_x - perp_x * base_width, sun_y - perp_y * base_width)
            p3 = (tip_x - perp_x * base_width * 0.15, tip_y - perp_y * base_width * 0.15)
            p4 = (tip_x + perp_x * base_width * 0.15, tip_y + perp_y * base_width * 0.15)

            pygame.draw.polygon(rays_surface, ray_color, [p1, p2, p3, p4])

        screen.blit(rays_surface, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)

    def get_status_text(self):
        hours = self.time_of_day * 24
        h = int(hours)
        m = int((hours - h) * 60)
        phase = "day" if self.is_daytime() else "night"
        return f"{phase} {h:02d}:{m:02d}"
