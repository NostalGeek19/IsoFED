class CameraRotationSystem:


    DRAG_THRESHOLD_PIXELS = 80.0

    def __init__(self, rotation_steps=0, drag_threshold=DRAG_THRESHOLD_PIXELS):
        self.rotation_steps = rotation_steps % 4   
        self.drag_threshold = drag_threshold
        self._dragging = False
        self._drag_accum = 0.0   

    # ------------------------------------------------------------------
    def start_drag(self):
        self._dragging = True
        self._drag_accum = 0.0

    def stop_drag(self):
        self._dragging = False
        self._drag_accum = 0.0

    def is_dragging(self):
        return self._dragging

    def accumulate_drag(self, delta_x_pixels):
        self._drag_accum += delta_x_pixels
        while self._drag_accum >= self.drag_threshold:
            self.rotation_steps = (self.rotation_steps + 1) % 4
            self._drag_accum -= self.drag_threshold
        while self._drag_accum <= -self.drag_threshold:
            self.rotation_steps = (self.rotation_steps - 1) % 4
            self._drag_accum += self.drag_threshold

    def set_drag_threshold(self, pixels):
        self.drag_threshold = max(1.0, pixels)

    # ------------------------------------------------------------------
    def rotate_step(self, steps=1):
        self.rotation_steps = (self.rotation_steps + steps) % 4
        return self.rotation_steps

    def set_rotation(self, rotation_steps):
        self.rotation_steps = int(rotation_steps) % 4

    def get_rotation_steps(self):
        return self.rotation_steps

    def get_angle_degrees(self):
        return self.rotation_steps * 90

    def is_default_orientation(self):
        return self.rotation_steps == 0

    def get_cache_key(self):
        return self.rotation_steps

    # ------------------------------------------------------------------
    def to_view_space(self, x, y):
        r = self.rotation_steps
        if r == 0:
            return x, y
        elif r == 1:
            return y, -x
        elif r == 2:
            return -x, -y
        else:  # r == 3
            return -y, x

    def to_world_space(self, vx, vy):
        r = self.rotation_steps
        if r == 0:
            return vx, vy
        elif r == 1:
            return -vy, vx
        elif r == 2:
            return -vx, -vy
        else:  # r == 3
            return vy, -vx

    # ------------------------------------------------------------------
    def get_status_text(self):
        suffix = " (drag to rotate)" if self._dragging else ""
        return f"{self.get_angle_degrees()}°{suffix}"