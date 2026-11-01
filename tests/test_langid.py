from subtalk.asr.langid import Decision, DecodeStats, LangEvidence, decide
from subtalk.config import LangIdCfg

CFG = LangIdCfg()


def d(text, lp=-0.3, ns=0.05, cr=1.4):
    return DecodeStats(text=text, avg_logprob=lp, no_speech_prob=ns, compression_ratio=cr)


def test_clear_mongolian():
    ev = LangEvidence(3.0, p_mn=0.9, p_ja=0.1, top_language="mn", unforced=d("Сайн байна уу"))
    r = decide(ev, CFG)
    assert r.kind == "MN" and r.text == "Сайн байна уу" and r.quality == "ok"


def test_clear_japanese():
    ev = LangEvidence(3.0, p_mn=0.05, p_ja=0.95, top_language="ja", unforced=d("皆さん、こんにちは"))
    assert decide(ev, CFG).kind == "JA"


def test_mongolian_detected_as_russian_uses_forced_decode():
    ev = LangEvidence(3.0, p_mn=0.8, p_ja=0.2, top_language="ru",
                      unforced=d("Сайн байна"), forced={"mn": d("Сайн байна уу")})
    r = decide(ev, CFG)
    assert r.kind == "MN" and r.text == "Сайн байна уу"


def test_low_margin_without_second_signal_is_uncertain():
    ev = LangEvidence(3.0, p_mn=0.55, p_ja=0.45, top_language="mn", unforced=d("..."))
    assert decide(ev, CFG).kind == "UNCERTAIN"


def test_forced_logprob_disagreement_is_uncertain():
    ev = LangEvidence(3.0, p_mn=0.7, p_ja=0.3, top_language="mn", unforced=d("тест"),
                      forced={"mn": d("тест", lp=-1.5), "ja": d("テスト", lp=-0.2)})
    r = decide(ev, CFG)
    assert r.kind == "UNCERTAIN" and "lid_logprob_disagree" in r.reasons


def test_forced_logprob_can_confirm_weak_lid():
    ev = LangEvidence(3.0, p_mn=0.6, p_ja=0.4, top_language="mn", unforced=d("Сайн"),
                      forced={"mn": d("Сайн байна", lp=-0.2), "ja": d("さい", lp=-1.4)})
    assert decide(ev, CFG).kind == "MN"


def test_short_segment_needs_bigger_margin():
    ev = LangEvidence(1.4, p_mn=0.7, p_ja=0.3, top_language="mn", unforced=d("Тийм"))
    assert decide(ev, CFG).kind == "UNCERTAIN"


def test_too_short_is_nonspeech():
    assert decide(LangEvidence(0.5, 0.9, 0.1, "mn", d("а")), CFG).kind == "NONSPEECH"


def test_music_no_speech_prob():
    ev = LangEvidence(5.0, 0.6, 0.4, "mn", unforced=d("ла ла ла", ns=0.9))
    assert decide(ev, CFG).kind == "NONSPEECH"


def test_hallucination_pattern_dropped():
    ev = LangEvidence(4.0, 0.1, 0.9, "ja", unforced=d("ご視聴ありがとうございました"))
    assert decide(ev, CFG).kind == "NONSPEECH"


def test_repetition_loop_dropped_by_compression():
    ev = LangEvidence(4.0, 0.9, 0.1, "mn", unforced=d("баяр баяр баяр баяр", cr=3.1))
    assert decide(ev, CFG).kind == "NONSPEECH"


def test_mixed_scripts_overlap():
    ev = LangEvidence(4.0, 0.6, 0.4, None, unforced=d("Сайн байна уу 皆さんこんにちは ありがとう"))
    assert decide(ev, CFG).kind == "OVERLAP"


def test_low_logprob_marks_low_quality():
    ev = LangEvidence(3.0, 0.95, 0.05, "mn", unforced=d("бүрхэг яриа", lp=-1.2))
    r = decide(ev, CFG)
    assert r.kind == "MN" and r.quality == "low"
