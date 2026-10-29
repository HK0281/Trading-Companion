"""Boundary checks use independent typed evidence, including equal values for different roles."""
import unittest
from trading_companion.answer_validation import check_claim, normalize_horizon, HARD, NUMERIC


def context():
    def row(identifier, label, value, unit, day="2026-09-25"):
        return dict(id=identifier, instrument_id="000001.SZ", label=label, raw_value=value, unit=unit,
                    display=str(value), date=day, content_kind="fact")
    return dict(start_date="2026-01-01", daily_cutoff="2026-09-25",
        securities=[dict(instrument_id="000001.SZ", name="平安银行"), dict(instrument_id="600000.SH", name="浦发银行")],
        evidence=[row("E1", "最新收盘价", 29.7, "CNY/share"), row("E2", "营业收入", 5346000000, "cny"),
                  row("E3", "MA20", 29.1, "CNY/share"), row("E4", "营收同比", -5.24, "percent"),
                  {**row("E5", "最新收盘价", 99, "CNY/share"), "instrument_id": "600000.SH"}])


class ValidationTests(unittest.TestCase):
    def check(self, text, **extra):
        return check_claim(dict(text=text, **extra), instrument="000001.SZ", context=context(), section="current", index=0)

    def test_rounding_conversion_and_periods_need_no_visible_citation(self):
        for text in ("收盘约30元。", "收盘29.80元。", "营业收入约53亿元。", "营收同比-5.2%。",
                     "未来1个月，近20日走势仍需观察。", "若收盘跌破20日均线29.1元则重评。", "目标价可参考计划表中的止盈1。"):
            with self.subTest(text=text):
                kept, _, issues, *_ = self.check(text)
                self.assertEqual(issues, [])
                self.assertEqual(kept, text)

    def test_wrong_values_roles_currency_sign_and_other_company_do_not_pass(self):
        for text in ("收盘30.5元。", "收盘99元。", "营业收入53万元。", "营收同比5.24%。", "止损29.7元。",
                     "MA20为29.7元。", "2026-09-26收盘29.7元。", "未知读数12345。"):
            with self.subTest(text=text):
                kept, _, issues, *_ = self.check(text)
                self.assertFalse(kept)
                self.assertEqual(issues[0].code, NUMERIC)

    def test_a_wrong_sentence_does_not_erase_the_other_sentence(self):
        text, _, issues, generated, kept = self.check("走势仍有不确定性。收盘999元。")
        self.assertEqual((text, generated, kept), ("走势仍有不确定性。", 2, 1))
        self.assertEqual(len(issues), 1)

    def test_both_range_endpoints_and_date_binding_are_checked(self):
        self.assertFalse(self.check("收盘区间29.7—99元。")[0])
        self.assertTrue(self.check("收盘区间29.7—30元。")[0])
        self.assertTrue(self.check("收盘区间29.7-30元。")[0])
        self.assertFalse(self.check("2026-09-24收盘29.7元。")[0])
        self.assertFalse(self.check("收盘区间30—29.7元。")[0])
        self.assertTrue(self.check("营业收入5346000000元。")[0])

    def test_hard_content_is_local_and_never_in_public_issue(self):
        for text in ("一定上涨。", "<script>alert(1)</script>", "已为你下单。", "上涨概率70%。", "浦发银行收盘29.7元。"):
            with self.subTest(text=text):
                kept, _, issues, *_ = self.check(text + ("" if text.endswith("。") else "。") + "需要观察相反证据。")
                self.assertEqual(kept, "需要观察相反证据。")
                self.assertEqual(issues[0].code, HARD)
                self.assertNotIn(text, str(issues[0].public()))

    def test_horizon_synonyms_do_not_change_the_data_cutoff(self):
        self.assertEqual({normalize_horizon(s) for s in ("一个月", "未来1个月", "下个月")}, {(1, "月")})
        self.assertNotEqual(normalize_horizon("两个月"), normalize_horizon("一个月"))

    def test_signed_changes_currency_and_chinese_numbers_do_not_bypass_matching(self):
        self.assertTrue(self.check("营收同比下降5.2%。")[0])
        for text in ("营收同比增长5.2%。", "营收同比增长-5.2%。", "收盘三百元。", "收盘29.7美元。", "营收同比下降5.2个百分点。"):
            with self.subTest(text=text): self.assertFalse(self.check(text)[0])

    def test_neutral_return_label_does_not_imply_a_decline(self):
        for value in ("4.00", "-4.00"):
            c = context()
            c["evidence"].append(dict(id="E6", instrument_id="000001.SZ", label="近20日涨跌幅",
                raw_value=value, unit="percent", display=value + "%", date="2026-09-25", content_kind="fact"))
            args = dict(instrument="000001.SZ", context=c, section="current", index=0)
            text = f"近20日涨跌幅为{value}%。"
            self.assertEqual(check_claim(text, **args)[0], text)
            opposite = "-4.00" if value == "4.00" else "4.00"
            self.assertFalse(check_claim(f"近20日涨跌幅为{opposite}%。", **args)[0])
            # An explicit decline still uses a positive magnitude for a negative return.
            self.assertEqual(bool(check_claim("近20日跌幅为4.00%。", **args)[0]), value == "-4.00")

    def test_each_price_binds_to_its_own_date_and_tiny_price_is_bounded(self):
        c = context()
        c["evidence"].append({**c["evidence"][0], "id": "E6", "raw_value": 28, "date": "2026-09-24"})
        args = dict(instrument="000001.SZ", context=c, section="current", index=0)
        self.assertTrue(check_claim("2026-09-24收盘28元，2026-09-25收盘29.7元。", **args)[0])
        self.assertFalse(check_claim("2026-09-24收盘29.7元，2026-09-25收盘28元。", **args)[0])
        self.assertFalse(check_claim("当前收盘28元。", **args)[0])
        c["evidence"][0]["raw_value"] = "0.001"
        self.assertFalse(check_claim("收盘0.01元。", **args)[0])

