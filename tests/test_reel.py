import struct
import unittest
import uuid

from t3_lametric.reel import roll_images, stream_packet


class ReelTests(unittest.TestCase):
    def test_only_changed_digits_move_and_stat_icon_stays_fixed(self):
        frames = roll_images("136.2M", "136.3M")
        self.assertEqual(len(frames), 9)
        self.assertNotEqual(frames[0], frames[-1])
        for frame in frames:
            self.assertEqual(len(frame), 37 * 8)
            for y in range(8):
                self.assertEqual(frame[y * 37 : y * 37 + 24], frames[0][y * 37 : y * 37 + 24])
                self.assertEqual(
                    frame[y * 37 + 27 : (y + 1) * 37], frames[0][y * 37 + 27 : (y + 1) * 37]
                )

    def test_unit_width_changes_and_equal_values_skip_the_reel(self):
        for old, new in (("999.9k", "1.0M"), ("9.9M", "10.0M"), ("1.0M", "1.0M")):
            self.assertEqual(roll_images(old, new), [])

    def test_lmsp_packet_contains_the_37_by_8_raw_pixels_and_session(self):
        session = uuid.UUID("12345678-1234-5678-1234-567812345678")
        pixels = roll_images("1.0M", "1.1M")[0]
        packet = stream_packet(str(session), pixels)
        self.assertEqual(packet[:6], b"lmsp\x01\x00")
        self.assertEqual(packet[6:22], session.bytes)
        self.assertEqual(packet[22:26], bytes((0, 0, 1, 0)))
        self.assertEqual(struct.unpack("<HHHHH", packet[26:36]), (0, 0, 37, 8, 37 * 8 * 3))
        self.assertEqual(len(packet), 924)
        self.assertEqual(packet[36:39], b"\0\0\0")
