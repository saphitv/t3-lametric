import unittest
from datetime import timedelta

from fixtures import NOW

from t3_lametric.counter import TokenCounter
from t3_lametric.display import compact


class CounterTests(unittest.TestCase):
    def test_pixel_reel_uses_exact_target_and_only_starts_once_per_change(self):
        counter = TokenCounter()
        counter.render(1_000_000, "", NOW.date(), 0, rolling=True)
        self.assertEqual(counter.render(1_100_000, "", NOW.date(), 1, rolling=True), "1.1M")
        self.assertEqual(counter.roll_from, "1.0M")
        self.assertIsNone(counter.deadline)
        counter.render(1_100_000, "", NOW.date(), 1.5, rolling=True)
        self.assertIsNone(counter.roll_from)
        counter.pause()
        counter.render(1_200_000, "", NOW.date(), 2, rolling=True)
        self.assertIsNone(counter.roll_from)

    def test_decimal_is_always_present_including_zero_and_unit_boundaries(self):
        examples = {
            0: "0.0",
            12: "12.0",
            999: "999.0",
            1000: "1.0k",
            999_950: "1.0M",
            10_000_000: "10.0M",
            2_000_000_000: "2.0B",
        }
        for count, expected in examples.items():
            with self.subTest(count=count):
                self.assertEqual(compact(count), expected)

    def test_countup_is_bounded_monotone_and_settles_on_exact_formatted_total(self):
        counter = TokenCounter()
        self.assertEqual(counter.render(1000, "", NOW.date(), 0), "1.0k")
        values = [counter.render(1900, "", NOW.date(), t) for t in (1, 1.18, 1.36, 2)]
        self.assertEqual(values, ["1.3k", "1.6k", "1.9k", "1.9k"])
        self.assertIsNone(counter.deadline)

    def test_no_replay_after_pause_midnight_or_corrected_lower_total(self):
        counter = TokenCounter()
        counter.render(1000, "", NOW.date(), 0)
        counter.render(1900, "", NOW.date(), 1)
        self.assertIsNotNone(counter.deadline)
        counter.pause()
        self.assertEqual(counter.render(2000, "", NOW.date(), 2), "2.0k")
        self.assertIsNone(counter.deadline)
        self.assertEqual(counter.render(500, "", NOW.date(), 3), "500.0")
        tomorrow = (NOW + timedelta(days=1)).date()
        self.assertEqual(counter.render(3000, "", tomorrow, 4), "3.0k")
        self.assertIsNone(counter.deadline)

    def test_small_or_identical_rounded_changes_do_not_generate_animation_requests(self):
        counter = TokenCounter()
        counter.render(1_000_000, "+", NOW.date(), 0)
        self.assertEqual(counter.render(1_000_001, "+", NOW.date(), 1), "1.0M+")
        self.assertIsNone(counter.deadline)
        self.assertEqual(counter.render(1_100_000, "+", NOW.date(), 2), "1.1M+")
        self.assertIsNone(counter.deadline)
        self.assertEqual(counter.render(1_100_000, "", NOW.date(), 3), "1.1M")


if __name__ == "__main__":
    unittest.main()
