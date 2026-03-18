# epd2in13_V2 — V2 aligned with V4, usable area 120px centered in 122px
# - Full 122x250 windowing
# - Data entry: X++ then Y++ (0x03) like V3/V4
# - getbuffer() accepts 120x250 (or 122x250) image and centers it (offset=1)
# - No rotation/mirroring on driver side (handled upstream if needed)
# - No 1px wrap-around offset (fixes the dark line artifact)

import logging
import time
from . import epdconfig
from logger import Logger

# Physical panel resolution (hardware)
EPD_WIDTH  = 122
EPD_HEIGHT = 250

logger = Logger(name="epd2in13_V2.py", level=logging.DEBUG)

class EPD:
    def __init__(self):
        self.is_initialized = False
        # Defensive timeout/logging for BUSY pin stalls.
        self.busy_timeout_s = 30.0
        self.busy_poll_ms = 50
        self.busy_log_interval_s = 5.0
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        
    FULL_UPDATE = 0
    PART_UPDATE = 1

    # Original Waveshare LUTs
    lut_full_update= [
        0x80,0x60,0x40,0x00,0x00,0x00,0x00,
        0x10,0x60,0x20,0x00,0x00,0x00,0x00,
        0x80,0x60,0x40,0x00,0x00,0x00,0x00,
        0x10,0x60,0x20,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,

        0x03,0x03,0x00,0x00,0x02,
        0x09,0x09,0x00,0x00,0x02,
        0x03,0x03,0x00,0x00,0x02,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,

        0x15,0x41,0xA8,0x32,0x30,0x0A,
    ]

    lut_partial_update = [
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,
        0x80,0x00,0x00,0x00,0x00,0x00,0x00,
        0x40,0x00,0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,

        0x0A,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x00,

        0x15,0x41,0xA8,0x32,0x30,0x0A,
    ]
        
    # Hardware reset
    def reset(self):
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(200) 
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(5)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(200)   

    def send_command(self, command):
        epdconfig.digital_write(self.dc_pin, 0)
        epdconfig.spi_writebyte([command])

    def send_data(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.spi_writebyte([data])

    def send_data2(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.spi_writebyte2(data)
        
    def ReadBusy(self):
        # 0: idle, 1: busy
        started = time.monotonic()
        last_log = started
        while epdconfig.digital_read(self.busy_pin) == 1:
            now = time.monotonic()
            waited = now - started
            if waited >= self.busy_timeout_s:
                raise TimeoutError(
                    f"EPD busy timeout after {self.busy_timeout_s:.1f}s "
                    f"(pin={self.busy_pin}, state=1, expected idle=0)"
                )
            if (now - last_log) >= self.busy_log_interval_s:
                logger.warning(
                    f"ReadBusy waiting {waited:.1f}s (pin={self.busy_pin}, state=1/busy)"
                )
                last_log = now
            epdconfig.delay_ms(self.busy_poll_ms)

    def TurnOnDisplay(self):
        self.send_command(0x22)
        self.send_data(0xC7)
        self.send_command(0x20)        
        self.ReadBusy()
        
    def TurnOnDisplayPart(self):
        self.send_command(0x22)
        self.send_data(0x0c)
        self.send_command(0x20)        
        self.ReadBusy()
        
    def init(self, update):
        """
        Init V2 aligned with V4:
        - Data entry: 0x03 (X++ then Y++)
        - X-window:   start=0x00, end=0x0F (16 bytes = 128 bits => covers 122 px)
        - Y-window:   start=0x0000, end=0x00F9 (250 lines)
        - Cursor:     X=0x00, Y=0x0000
        """
        if not self.is_initialized:
            if epdconfig.module_init() != 0:
                return -1
            self.reset()
            self.is_initialized = True

        if update == self.FULL_UPDATE:
            self.ReadBusy()
            self.send_command(0x12)  # soft reset
            self.ReadBusy()

            # Analog/Digital blocks
            self.send_command(0x74); self.send_data(0x54)
            self.send_command(0x7E); self.send_data(0x3B)

            # Driver output control (height - 1) => 249
            self.send_command(0x01)
            self.send_data(0xF9)  # 249
            self.send_data(0x00)
            self.send_data(0x00)

            # Data entry mode X++ Y++
            self.send_command(0x11)
            self.send_data(0x03)

            # RAM X window (bytes) 0..15 (16*8=128 bits -> covers 122 px)
            self.send_command(0x44)
            self.send_data(0x00)  # start
            self.send_data(0x0F)  # end

            # RAM Y window 0..249
            self.send_command(0x45)
            self.send_data(0x00)  # Y-start L
            self.send_data(0x00)  # Y-start H
            self.send_data(0xF9)  # Y-end L
            self.send_data(0x00)  # Y-end H

            # Border/VCOM/LUT timing
            self.send_command(0x3C); self.send_data(0x03)
            self.send_command(0x2C); self.send_data(0x55)

            self.send_command(0x03); self.send_data(self.lut_full_update[70])
            self.send_command(0x04)
            self.send_data(self.lut_full_update[71])
            self.send_data(self.lut_full_update[72])
            self.send_data(self.lut_full_update[73])
            self.send_command(0x3A); self.send_data(self.lut_full_update[74])  # Dummy line
            self.send_command(0x3B); self.send_data(self.lut_full_update[75])  # Gate time

            self.send_command(0x32)  # LUT table
            for i in range(70):
                self.send_data(self.lut_full_update[i])

            # X/Y cursor
            self.send_command(0x4E); self.send_data(0x00)      # X-counter (byte)
            self.send_command(0x4F); self.send_data(0x00); self.send_data(0x00)  # Y-counter
            self.ReadBusy()

        else:
            # PARTIAL init
            self.send_command(0x2C); self.send_data(0x26)   # VCOM
            self.ReadBusy()

            self.send_command(0x32)
            for i in range(70):
                self.send_data(self.lut_partial_update[i])

            self.send_command(0x37)
            self.send_data(0x00); self.send_data(0x00); self.send_data(0x00)
            self.send_data(0x00); self.send_data(0x40); self.send_data(0x00); self.send_data(0x00)

            self.send_command(0x22); self.send_data(0xC0)
            self.send_command(0x20); self.ReadBusy()

            self.send_command(0x3C); self.send_data(0x01)

            # Same windowing as full update
            self.send_command(0x44); self.send_data(0x00); self.send_data(0x0F)
            self.send_command(0x45); self.send_data(0x00); self.send_data(0x00); self.send_data(0xF9); self.send_data(0x00)
            self.send_command(0x4E); self.send_data(0x00)
            self.send_command(0x4F); self.send_data(0x00); self.send_data(0x00)

        return 0


    def getbuffer(self, image):
        W, H = self.width, self.height   # 122 x 250
        bytes_per_line = (W + 7) // 8    # 16
        buf = bytearray([0xFF] * (bytes_per_line * H))

        img = image.convert('1')
        imw, imh = img.size

        work_w = min(imw, 120)
        x_offset = (W - work_w) // 2  # =1 pour 120px

        pixels = img.load()
        for y in range(min(imh, H)):
            base = y * bytes_per_line
            for x in range(work_w):
                src_x = x if imw == 120 else (x + (imw - work_w)//2)
                if pixels[src_x, y] == 0:
                    xi = x + x_offset
                    if xi <= 0 or xi >= W-1:
                        continue  # safety: never write to col 0 or 121
                    byte_index = base + (xi >> 3)
                    bit = 0x80 >> (xi & 7)
                    buf[byte_index] &= (~bit) & 0xFF

            # force columns 0 and 121 to white
            buf[base + (0 >> 3)] |= (0x80 >> (0 & 7))
            buf[base + (121 >> 3)] |= (0x80 >> (121 & 7))

        return buf


        
    def display(self, image):
        self.send_command(0x24)
        self.send_data2(image)   
        self.TurnOnDisplay()
        
    def displayPartial(self, image):
        bytes_per_line = (self.width + 7) // 8
        total = self.height * bytes_per_line
        # Inverted buffer for the second plane (as per original)
        buf_inv = bytearray(total)
        for i in range(total):
            buf_inv[i] = (~image[i]) & 0xFF

        self.send_command(0x24)
        self.send_data2(image)               
        self.send_command(0x26)
        self.send_data2(buf_inv)
        self.TurnOnDisplayPart()

    def displayPartBaseImage(self, image):
        self.send_command(0x24)
        self.send_data2(image)   
        self.send_command(0x26)
        self.send_data2(image)  
        self.TurnOnDisplay()
    
    def Clear(self, color=0xFF):
        bytes_per_line = (self.width + 7) // 8
        buf = bytearray([color] * (self.height * bytes_per_line))
        self.send_command(0x24)
        self.send_data2(buf)
        self.TurnOnDisplay()

    def sleep(self):
        self.send_command(0x10) # enter deep sleep
        self.send_data(0x03)
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()

# END OF FILE
