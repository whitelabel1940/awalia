

def test_casual_labels_are_loose_but_keep_the_scale():
    from cbs import clock
    cur = "2026-10-15T20:00:00+09:00"
    cases = {"2026-10-14T10:00:00+09:00": "昨日", "2026-10-13T10:00:00+09:00": "おととい", "2026-10-10T10:00:00+09:00": "この間",
             "2026-10-05T10:00:00+09:00": "先週", "2026-09-25T10:00:00+09:00": "ちょっと前", "2026-09-01T10:00:00+09:00": "1か月くらい前",
             "2026-08-05T10:00:00+09:00": "夏ごろ", "2025-12-20T10:00:00+09:00": "去年の冬ごろ", "2026-10-17T10:00:00+09:00": "あさって",
             "2026-10-24T10:00:00+09:00": "来週"}
    for value, label in cases.items():
        assert clock.casual(value, cur) == label
    assert clock.stamp("2026-08-05T10:00:00+09:00", cur)["casual"] == "夏ごろ"
    assert clock.casual_gap("2026-10-15T08:00:00+09:00", cur) is None
    assert clock.casual_gap("2026-10-08T10:00:00+09:00", cur) == "1週間ぶりくらい"
    assert clock.casual_gap("2026-08-05T10:00:00+09:00", cur) == "2か月ぶりくらい"
