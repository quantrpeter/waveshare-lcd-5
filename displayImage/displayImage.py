import lcd_bus
import machine
from time import sleep
import rgb_display
import lvgl as lv
import task_handler
from fs_driver import fs_register

# Display settings for Waveshare ESP32-S3-Touch-LCD-5B (1024x600, 16-bit RGB565 parallel)
# Pin mapping and timing from Waveshare's ESP-IDF demo (09_lvgl_v9_demo):
#   HSYNC=46, VSYNC=3, DE=5, PCLK=7
#   B3..B7 = 14, 38, 18, 17, 10
#   G2..G7 = 39, 0, 45, 48, 47, 21
#   R3..R7 = 1, 2, 42, 41, 40
#   I2C SDA=8, SCL=9 (IO expander + GT911 touch)
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


def _init_io_expander(i2c):
    devices = i2c.scan()
    print("I2C scan:", [hex(addr) for addr in devices])

    on = _EXIO_TP_RST | _EXIO_LCD_BL | _EXIO_LCD_RST
    in_reset = _EXIO_TP_RST | _EXIO_LCD_BL

    if _CH422G_OUT in devices:
        print("IO expander: CH422G")
        i2c.writeto(_CH422G_SYS, b"\x01")  # enable push-pull outputs
        i2c.writeto(_CH422G_OUT, bytes([in_reset]))
        sleep(0.02)
        i2c.writeto(_CH422G_OUT, bytes([on]))
    elif _IOEXT_ADDR in devices:
        print("IO expander: CH32V003 IO extension")
        i2c.writeto(_IOEXT_ADDR, bytes([_IOEXT_REG_MODE, 0xFF]))  # all outputs
        i2c.writeto(_IOEXT_ADDR, bytes([_IOEXT_REG_OUT, in_reset]))
        sleep(0.02)
        i2c.writeto(_IOEXT_ADDR, bytes([_IOEXT_REG_OUT, on]))
    else:
        print("No IO expander found, backlight may stay off")
        return

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

print("Display ready")

th = task_handler.TaskHandler()

scrn = lv.screen_active()
scrn.set_style_bg_color(lv.color_hex(0x101820), 0)

status = lv.label(scrn)
status.set_text("Display init OK")
status.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
status.align(lv.ALIGN.TOP_MID, 0, 16)

print("Registering filesystem...")
fs_drv = lv.fs_drv_t()
fs_register(fs_drv, "S")

try:
    print("Loading image...")
    img = lv.image(scrn)
    img.set_src("S:semiblock_logo_2.png")
    img.align(lv.ALIGN.CENTER, 0, 24)
    print("Image displayed successfully")
except Exception as exc:
    print("Image load failed:", exc)
    status.set_text("Display init OK\nImage load failed")

lv.task_handler()
lv.refr_now(None)

print("Screen set up, entering main loop...")

while True:
    lv.task_handler()
    sleep(0.1)
