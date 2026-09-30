"""Standard-library-only checks for app/adjudication.py.

Run with either:
    python3 tests/test_adjudication.py
    pytest tests/test_adjudication.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adjudication import Scan, Segment, adjudicate_runner  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
CUTOFF = T0 + timedelta(hours=1)


def segments(minutes):
    # minutes: list of (min, max) pairs per segment
    return [
        Segment(position=i + 1, min_seconds=lo * 60, max_seconds=hi * 60)
        for i, (lo, hi) in enumerate(minutes)
    ]


def sc(no, pos, minute, second=0, offset=0):
    return Scan(
        scan_no=no,
        point_position=pos,
        occurred_at=T0 + timedelta(minutes=minute, seconds=second),
        offset_seconds=offset,
    )


def check(name, got, want):
    if got != want:
        raise AssertionError(f"{name}:\n  got={got!r}\n want={want!r}")


def test_happy_path_out_of_order_submission():
    segs = segments([(5, 30), (5, 30), (5, 30)])
    scans = [sc("30", 3, 30), sc("10", 1, 10), sc("20", 2, 20)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("status", r.status, "finished")
    check("finish", r.finish_time, sc("30", 3, 30).occurred_at)
    check("total", r.total_seconds, 1800.0)
    check("used", [u.scan_no for u in r.used_scans], ["10", "20", "30"])


def test_earliest_finish_wins():
    segs = segments([(5, 40), (5, 40)])
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 20),   # finishes 20
        sc("b1", 1, 15), sc("b2", 2, 25),   # finishes 25
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("earliest", [u.scan_no for u in r.used_scans], ["a1", "a2"])

    # Cross paths: a1(10)->b2(15, only 5 min gap, valid) finishes 15 < 20
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 30),
        sc("b1", 1, 20), sc("b2", 2, 15),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("cross", [u.scan_no for u in r.used_scans], ["a1", "b2"])


def test_lex_tiebreak_on_equal_finish():
    segs = segments([(5, 40), (5, 40), (5, 40)])
    scans = [
        sc("9", 1, 10), sc("10", 1, 10),
        sc("19", 2, 20), sc("20", 2, 20),
        sc("29", 3, 30), sc("30", 3, 30),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    # All eight combinations finish at the same time; the lex-min tuple of
    # scan numbers wins ("10" < "9" lexically, then "19" < "20", then "29" < "30").
    check("lex", [u.scan_no for u in r.used_scans], ["10", "19", "29"])
    check("finish", r.finish_time, sc("30", 3, 30).occurred_at)


def test_equal_timestamps_between_points_rejected():
    segs = segments([(5, 40), (5, 40)])
    scans = [sc("1", 1, 10), sc("2", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("equal ts dnf", r.status, "did_not_finish")


def test_segment_outside_interval_dnf():
    segs = segments([(5, 9), (5, 30)])
    scans = [sc("1", 1, 10), sc("2", 2, 15)]  # first segment 10 > max 9
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("over max dnf", r.status, "did_not_finish")

    segs2 = segments([(11, 30), (5, 30)])
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs2, scans=scans)
    check("under min dnf", r.status, "did_not_finish")


def test_closed_interval_boundaries_inclusive():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 5), sc("2", 2, 30)]  # exactly min, then exactly max
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("boundary", [u.scan_no for u in r.used_scans], ["1", "2"])
    check("boundary secs", [u.segment_seconds for u in r.used_scans], [300.0, 1500.0])


def test_missing_finish_scan_dnf():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("no finish", r.status, "did_not_finish")
    check("no finish used", r.used_scans, ())


def test_finish_after_cutoff_dnf():
    segs = segments([(5, 60), (5, 60)])
    scans = [sc("1", 1, 20), sc("2", 2, 61)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("after cutoff", r.status, "did_not_finish")

    # exactly at cutoff is allowed ("not later than")
    scans = [sc("1", 1, 20), sc("2", 2, 60)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("at cutoff", r.status, "finished")
    check("at cutoff time", r.finish_time, CUTOFF)


def test_irregular_scan_before_start_dnf():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("early", 1, -1), sc("late", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("before start dnf", r.status, "did_not_finish")


def test_first_segment_zero_lower_bound_at_start_instant():
    segs = segments([(0, 30), (5, 30)])
    scans = [sc("1", 1, 0), sc("2", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("zero min at start", r.status, "finished")
    check("zero min secs", r.used_scans[0].segment_seconds, 0.0)
    # even with a 0 lower bound, two scans at the same instant across points
    # violate strict increase between consecutive scans
    scans = [sc("1", 1, 5), sc("2", 2, 5)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("equal ts still dnf", r.status, "did_not_finish")


def test_irrelevant_point_scans_ignored():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 10), sc("x", 5, 12), sc("2", 2, 20)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("ignore", [u.scan_no for u in r.used_scans], ["1", "2"])


def test_point_offset_calibrates_segment_and_finish():
    # Raw path 10/20/30 min is valid; with the FIN clock 120s slow the
    # calibrated finish lands at 32 min: segment and finish move together.
    segs = segments([(5, 40), (5, 40), (5, 40)])
    scans = [
        sc("10", 1, 10),
        sc("20", 2, 20),
        sc("30", 3, 30, offset=120),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("status", r.status, "finished")
    check("calibrated finish", r.finish_time,
          T0 + timedelta(minutes=32))
    check("total calibrated", r.total_seconds, 32 * 60.0)
    fin = r.used_scans[2]
    # Raw time is retained, calibrated time and the offset are shown.
    check("raw kept", fin.occurred_at, T0 + timedelta(minutes=30))
    check("calibrated shown", fin.calibrated_at, T0 + timedelta(minutes=32))
    check("offset shown", fin.offset_seconds, 120)
    check("calibrated segment secs", fin.segment_seconds, 12 * 60.0)


def test_offset_rescues_out_of_interval_and_cutoff():
    # Raw FIN at 31 min violates both the 10-min C2->FIN max and the 30-min
    # cutoff; a +120s correction on the slow FIN device moves it to 29.
    segs = segments([(5, 30), (5, 30), (5, 10)])
    cutoff30 = T0 + timedelta(minutes=30)
    scans = [
        sc("10", 1, 10),
        sc("20", 2, 20),
        sc("31", 3, 31, offset=-120),
    ]
    r = adjudicate_runner(
        race_start=T0, cutoff=cutoff30, segments=segs, scans=scans
    )
    check("rescued", r.status, "finished")
    check("finish at cutoff minus 1min", r.finish_time,
          T0 + timedelta(minutes=29))


def test_negative_offset_can_invalidate_path():
    # Raw 10/20/30 is valid; C2 runs 120s fast, so calibrated C2 is 18 min:
    # C1->C2 is only 8 min (< 10 min lower bound) -> no valid path.
    segs = segments([(5, 30), (10, 30), (5, 30)])
    scans = [
        sc("10", 1, 10),
        sc("20", 2, 20, offset=-120),
        sc("30", 3, 30),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("offset dnf", r.status, "did_not_finish")


def test_strict_increase_uses_calibrated_times():
    # Raw timestamps increase by one minute, but offsets pull them to the
    # same calibrated instant -> strict increase fails -> DNF.
    segs = segments([(0, 30), (0, 30)])
    scans = [
        sc("1", 1, 10, offset=60),    # calibrated 11:00
        sc("2", 2, 11, offset=0),     # calibrated 11:00
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("calibrated tie dnf", r.status, "did_not_finish")


def test_earliest_finish_ranks_by_calibrated_time():
    # Two complete paths finish at the same point: "a" finishes at raw 24,
    # "b" at raw 23 but its device runs 120s fast (calibrated 25). The
    # calibrated-earliest path wins even though its raw time is later.
    segs = segments([(5, 40), (5, 40)])
    scans = [
        sc("a1", 1, 10),
        sc("a2", 2, 24),                       # calibrated 24
        sc("b1", 1, 10),
        sc("b2", 2, 23, offset=120),           # raw 23 -> calibrated 25
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("calibrated earliest wins",
          [u.scan_no for u in r.used_scans], ["a1", "a2"])


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
