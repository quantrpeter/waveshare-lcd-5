import lcd_bus
import machine
from time import sleep
import rgb_display
import lvgl as lv
import builtins
builtins.lv = lv
import task_handler
from fs_driver import fs_register
import random
import i2c
import gt911
import pointer_framework

# Display settings for Waveshare ESP32-S3-Touch-LCD-5B (1024x600, 16-bit RGB565 parallel)
# Pin mapping and timing from Waveshare's ESP-IDF demo (09_lvgl_v9_demo):
#   HSYNC=46, VSYNC=3, DE=5, PCLK=7
#   B3..B7 = 14, 38, 18, 17, 10
#   G2..G7 = 39, 0, 45, 48, 47, 21
#   R3..R7 = 1, 2, 42, 41, 40
#   I2C SDA=8, SCL=9 (IO expander + GT911 touch), touch INT=4
#   Backlight / LCD reset / touch reset are driven by the IO expander, not GPIOs:
#     EXIO1=TP_RST, EXIO2=LCD_BL, EXIO3=LCD_RST
_WIDTH = 1024
_HEIGHT = 600
_HSYNC = 46
_VSYNC = 3
_DE = 5
_PCLK = 7
_DATA_PINS = (
    14, 38, 18, 17, 10,       # B3..B7 -> data0..data4
    39, 0, 45, 48, 47, 21,    # G2..G7 -> data5..data10
    1, 2, 42, 41, 40,         # R3..R7 -> data11..data15
)
# Lower this (e.g. 16 MHz) if the picture drifts/tears; PSRAM bandwidth is the limit.
_LCD_FREQ = 21000000
_I2C_SDA = 8
_I2C_SCL = 9
_TOUCH_INT = 4
_DRAW_BUFFER_LINES = 20
# Same as Waveshare's demo; 600 must be divisible by this.
_BOUNCE_BUFFER_LINES = 10

# Older 5B boards use a CH422G expander, newer ones a CH32V003 "IO extension"
# chip. Both answer at 0x24; only the CH422G also answers at 0x38.
_CH422G_SYS = 0x24
_CH422G_OUT = 0x38
_IOEXT_ADDR = 0x24
_IOEXT_REG_MODE = 0x02
_IOEXT_REG_OUT = 0x03
_EXIO_TP_RST = 1 << 1
_EXIO_LCD_BL = 1 << 2
_EXIO_LCD_RST = 1 << 3

# Game constants (landscape 1024x600)
SCREEN_WIDTH = _WIDTH
SCREEN_HEIGHT = _HEIGHT
BIRD_X = 150
BIRD_SIZE = 40
PIPE_WIDTH = 80
PIPE_GAP = 200
GRAVITY = 0.7
JUMP_STRENGTH = -6
PIPE_SPEED = 5
MAX_PIPES = 4
_FONT = getattr(lv, "font_montserrat_24", lv.font_montserrat_16)


def _init_io_expander(i2c):
    devices = i2c.scan()
    print("I2C scan:", [hex(addr) for addr in devices])

    on = _EXIO_TP_RST | _EXIO_LCD_BL | _EXIO_LCD_RST
    in_reset = _EXIO_LCD_BL

    if _CH422G_OUT in devices:
        print("IO expander: CH422G")
        i2c.writeto(_CH422G_SYS, b"\x01")  # enable push-pull outputs

        def write_out(value):
            i2c.writeto(_CH422G_OUT, bytes([value]))
    elif _IOEXT_ADDR in devices:
        print("IO expander: CH32V003 IO extension")
        i2c.writeto(_IOEXT_ADDR, bytes([_IOEXT_REG_MODE, 0xFF]))  # all outputs

        def write_out(value):
            i2c.writeto(_IOEXT_ADDR, bytes([_IOEXT_REG_OUT, value]))
    else:
        print("No IO expander found, backlight may stay off")
        return

    # Hold touch INT low while TP_RST rises so the GT911 latches address 0x5D.
    touch_int = machine.Pin(_TOUCH_INT, machine.Pin.OUT, value=0)
    write_out(in_reset)
    sleep(0.02)
    write_out(on)
    sleep(0.01)
    touch_int.init(machine.Pin.IN)
    sleep(0.05)


def _allocate_draw_buffers(display_bus, width, lines, color_space):
    buf_size = width * lines * lv.color_format_get_size(color_space)
    print("Allocating draw buffer:", width, "x", lines, "=", buf_size, "bytes")

    for flags, label in (
        # Internal RAM first: the panel scans its frame buffer out of PSRAM,
        # so drawing in PSRAM too starves it and the picture shakes.
        (lcd_bus.MEMORY_INTERNAL | lcd_bus.MEMORY_DMA, "INTERNAL|DMA"),
        (lcd_bus.MEMORY_INTERNAL, "INTERNAL"),
        (lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA, "SPIRAM|DMA"),
        (lcd_bus.MEMORY_SPIRAM, "SPIRAM"),
    ):
        try:
            buf1 = display_bus.allocate_framebuffer(buf_size, flags)
            print("Framebuffer 1:", label)
        except MemoryError:
            continue

        buf2 = None
        try:
            buf2 = display_bus.allocate_framebuffer(buf_size, flags)
            print("Framebuffer 2:", label)
        except MemoryError:
            print("Second framebuffer unavailable, using single buffering")

        return buf1, buf2

    raise MemoryError("Unable to allocate draw buffers")

print("Opening I2C bus...")
_i2c = machine.SoftI2C(sda=machine.Pin(_I2C_SDA), scl=machine.Pin(_I2C_SCL), freq=400000)
_init_io_expander(_i2c)
_touch_addr = gt911.I2C_ADDR if gt911.I2C_ADDR in _i2c.scan() else 0x14

print("Initializing RGB display bus...")
_rgb_bus_args = {
    "hsync": _HSYNC,
    "vsync": _VSYNC,
    "de": _DE,
    "pclk": _PCLK,
    "data0": _DATA_PINS[0],
    "data1": _DATA_PINS[1],
    "data2": _DATA_PINS[2],
    "data3": _DATA_PINS[3],
    "data4": _DATA_PINS[4],
    "data5": _DATA_PINS[5],
    "data6": _DATA_PINS[6],
    "data7": _DATA_PINS[7],
    "data8": _DATA_PINS[8],
    "data9": _DATA_PINS[9],
    "data10": _DATA_PINS[10],
    "data11": _DATA_PINS[11],
    "data12": _DATA_PINS[12],
    "data13": _DATA_PINS[13],
    "data14": _DATA_PINS[14],
    "data15": _DATA_PINS[15],
    "freq": _LCD_FREQ,
    # Timing from Waveshare's official 1024x600 demo (waveshare_rgb_lcd_port.c)
    "hsync_front_porch": 170,
    "hsync_back_porch": 145,
    "hsync_pulse_width": 30,
    "vsync_front_porch": 12,
    "vsync_back_porch": 23,
    "vsync_pulse_width": 2,
    "pclk_active_low": True,
}

# Bounce buffers stop the picture shaking while the game animates. They need
# firmware with the `bounce_buffer_lines` option added to lcd_bus.RGBBus.
try:
    display_bus = lcd_bus.RGBBus(bounce_buffer_lines=_BOUNCE_BUFFER_LINES, **_rgb_bus_args)
    print("Bounce buffer:", _BOUNCE_BUFFER_LINES, "lines")
except TypeError:
    print("Firmware has no bounce buffer support, screen may shake")
    display_bus = lcd_bus.RGBBus(**_rgb_bus_args)

buf1, buf2 = _allocate_draw_buffers(
    display_bus,
    _WIDTH,
    _DRAW_BUFFER_LINES,
    lv.COLOR_FORMAT.RGB565,
)

print("Initializing RGB display...")
display = rgb_display.RGBDisplay(
    data_bus=display_bus,
    display_width=_WIDTH,
    display_height=_HEIGHT,
    frame_buffer1=buf1,
    frame_buffer2=buf2,
    backlight_pin=None,
    reset_pin=None,
    color_space=lv.COLOR_FORMAT.RGB565,
)
display.init()

print("Initializing GT911 touch at", hex(_touch_addr), "...")
i2c_bus = i2c.I2C.Bus(host=0, scl=_I2C_SCL, sda=_I2C_SDA)
touch_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=_touch_addr, reg_bits=gt911.BITS)
indev = gt911.GT911(touch_dev)
print("Touch driver initialized")

print("Display ready")

th = task_handler.TaskHandler()

# Create screen
scrn = lv.screen_active()
scrn.set_style_bg_color(lv.color_hex(0x87CEEB), 0)  # Sky blue background
scrn.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)

# Game variables
bird_y = float(SCREEN_HEIGHT // 2)
bird_velocity = 0.0
pipes = []
score = 0
game_over = False
game_started = False

# Create bird (using a circle)
bird = lv.obj(scrn)
bird.set_size(BIRD_SIZE, BIRD_SIZE)
bird.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
bird.set_style_radius(BIRD_SIZE // 2, 0)
bird.set_style_bg_color(lv.color_hex(0xFFD700), 0)
bird.set_style_border_width(2, 0)
bird.set_style_border_color(lv.color_hex(0xFF8C00), 0)
bird.set_pos(BIRD_X, int(bird_y))

# Create score label
score_label = lv.label(scrn)
score_label.set_text("Score: 0")
score_label.set_pos(16, 16)
score_label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
score_label.set_style_text_font(_FONT, 0)

# Create start/restart message
msg_label = lv.label(scrn)
msg_label.set_text("TAP TO START")
msg_label.align(lv.ALIGN.CENTER, 0, 150)
msg_label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
msg_label.set_style_text_font(_FONT, 0)

# Register filesystem driver
print("Registering filesystem...")
fs_drv = lv.fs_drv_t()
fs_register(fs_drv, "S")

jump_requested = False

def _make_pipe_obj():
    o = lv.obj(scrn)
    o.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    o.set_style_bg_color(lv.color_hex(0x228B22), 0)
    o.set_style_border_width(2, 0)
    o.set_style_border_color(lv.color_hex(0x006400), 0)
    o.add_flag(lv.obj.FLAG.HIDDEN)
    return o

class Pipe:
    def __init__(self):
        self.top = _make_pipe_obj()
        self.bottom = _make_pipe_obj()
        self.x = 0
        self.gap_y = 0
        self.scored = False
        self.active = False

    def activate(self, x, gap_y):
        self.x = x
        self.gap_y = gap_y
        self.scored = False
        self.active = True
        self.top.set_size(PIPE_WIDTH, gap_y)
        self.top.set_pos(x, 0)
        self.top.remove_flag(lv.obj.FLAG.HIDDEN)
        self.bottom.set_size(PIPE_WIDTH, SCREEN_HEIGHT - gap_y - PIPE_GAP)
        self.bottom.set_pos(x, gap_y + PIPE_GAP)
        self.bottom.remove_flag(lv.obj.FLAG.HIDDEN)

    def deactivate(self):
        self.active = False
        self.top.add_flag(lv.obj.FLAG.HIDDEN)
        self.bottom.add_flag(lv.obj.FLAG.HIDDEN)

    def update(self):
        self.x -= PIPE_SPEED
        self.top.set_pos(self.x, 0)
        self.bottom.set_pos(self.x, self.gap_y + PIPE_GAP)

    def is_off_screen(self):
        return self.x < -PIPE_WIDTH

    def collides_with_bird(self, bx, by):
        if bx + BIRD_SIZE > self.x and bx < self.x + PIPE_WIDTH:
            if by < self.gap_y or by + BIRD_SIZE > self.gap_y + PIPE_GAP:
                return True
        return False

pipe_pool = [Pipe() for _ in range(MAX_PIPES)]

def spawn_pipe():
    gap_y = random.randint(60, SCREEN_HEIGHT - PIPE_GAP - 60)
    for p in pipe_pool:
        if not p.active:
            p.activate(SCREEN_WIDTH, gap_y)
            return

def reset_game():
    global bird_y, bird_velocity, score, game_over, game_started
    bird_y = float(SCREEN_HEIGHT // 2)
    bird_velocity = 0.0
    bird.set_pos(BIRD_X, int(bird_y))
    for p in pipe_pool:
        if p.active:
            p.deactivate()
    score = 0
    score_label.set_text("Score: 0")
    game_over = False
    game_started = False
    msg_label.set_text("TAP TO START")

def bird_jump():
    global bird_velocity, game_started
    if not game_started:
        game_started = True
        msg_label.set_text("")
        spawn_pipe()
    if not game_over:
        bird_velocity = JUMP_STRENGTH

def touch_event_cb(event):
    global jump_requested
    code = event.get_code()
    if code == lv.EVENT.PRESSED or code == lv.EVENT.CLICKED:
        jump_requested = True

scrn.add_event_cb(touch_event_cb, lv.EVENT.PRESSED, None)
scrn.add_event_cb(touch_event_cb, lv.EVENT.CLICKED, None)

frame_count = 0
# Spawn a pipe every ~300 px of travel so 3 pipes are on screen at once.
pipe_spawn_interval = 300 // PIPE_SPEED

print("Game ready! Tap to start...")

lv.task_handler()
lv.refr_now(None)

while True:
    lv.task_handler()

    if jump_requested:
        jump_requested = False
        if game_over:
            reset_game()
        else:
            bird_jump()

    if game_started and not game_over:
        bird_velocity += GRAVITY
        bird_y += bird_velocity

        if bird_y < 0:
            bird_y = 0
            bird_velocity = 0
        elif bird_y > SCREEN_HEIGHT - BIRD_SIZE:
            game_over = True
            msg_label.set_text("GAME OVER! TAP TO RESTART")

        bird.set_pos(BIRD_X, int(bird_y))

        frame_count += 1
        if frame_count >= pipe_spawn_interval:
            spawn_pipe()
            frame_count = 0

        for p in pipe_pool:
            if not p.active:
                continue
            p.update()
            if p.collides_with_bird(BIRD_X, int(bird_y)):
                game_over = True
                msg_label.set_text("GAME OVER! TAP TO RESTART")
            if not p.scored and p.x + PIPE_WIDTH < BIRD_X:
                p.scored = True
                score += 1
                score_label.set_text(f"Score: {score}")
            if p.is_off_screen():
                p.deactivate()

    # The RGB bus keeps its own full frame buffer, so only the dirty
    # rectangles (bird, pipes, labels) need redrawing each frame.
    lv.refr_now(None)
    sleep(0.02)
