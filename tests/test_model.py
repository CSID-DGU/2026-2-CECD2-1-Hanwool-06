"""Model inputs retain unknown values and do not hide genuine usage spikes."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from back.ml.lightgbm import metric, utils
from back.ml.lightgbm.main import run
from back.pipelines.refresh import build_calendar
from back.scripts.dataset_etl.build_clean_dataset import build_bills_bimonthly

ROOT = Path(__file__).resolve().parents[1]


class ModelDataTest(unittest.TestCase):
    def test_large_valid_reading_remains_an_alert_not_a_data_error(self):
        valid = pd.DataFrame({'고객번호': ['1'] * 3, '일사용량_톤': [9., 10., 11.]})
        calibration = metric.fit_calibration(valid, np.array([10.] * 3))
        observations = pd.DataFrame({'고객번호': ['1'] * 4, '일사용량_톤': [3186., -1., np.nan, np.inf]})
        result = metric.classify(metric.score_predictions(observations, np.array([10.] * 4), calibration), 2, 3)
        self.assertEqual(result['likely_data_error'].tolist(), [False, True, True, True])
        self.assertEqual(result.iloc[0]['심각도'], '경고')

    def test_common_shift_follows_the_other_meters_and_never_the_meter_itself(self):
        cfg = {'postprocess': {'common_shift_weight': 0.9, 'common_shift_min_peers': 6}}
        pred = np.full(12, 40.)
        meta = pd.DataFrame({'고객번호': [str(i) for i in range(12)], '날짜': [pd.Timestamp('2026-07-05')] * 12,
                             '일사용량_톤': np.full(12, 20.)})
        shifted = utils.apply_common_shift(meta, pred, pred, cfg)
        self.assertTrue(np.allclose(shifted, 22.))   # every meter used half of its prediction: 40 + 0.9 * (-0.5) * 40
        spike = meta.copy()
        spike.loc[0, '일사용량_톤'] = 4000.
        self.assertTrue(np.array_equal(utils.apply_common_shift(spike, pred, pred, cfg), shifted))
        self.assertTrue(np.array_equal(utils.apply_common_shift(meta.iloc[:6], pred[:6], pred[:6], cfg), pred[:6]))
        self.assertTrue(np.array_equal(utils.apply_common_shift(meta, pred, pred, {'postprocess': {}}), pred))

    def test_score_is_relative_to_the_recent_level_and_small_samples_lean_on_the_pooled_scale(self):
        rng = np.random.default_rng(0)
        valid = pd.DataFrame({'고객번호': ['a'] * 100 + ['b'] * 100 + ['c'] * 3,
                              '일사용량_톤': np.r_[20 + rng.normal(0, 2, 100), 60 + rng.normal(0, 6, 100), [20., 20.1, 19.9]]})
        level = np.r_[np.full(100, 20.), np.full(100, 60.), np.full(3, 20.)]
        calibration = metric.fit_calibration(valid, level, level)
        self.assertAlmostEqual(calibration.loc['a', 'scale'] / calibration.loc['b', 'scale'], 1, delta=0.25)
        self.assertGreater(calibration.loc['c', 'scale'], 0.5 * calibration.loc['a', 'scale'])
        rows = pd.DataFrame({'고객번호': ['a', 'b'], '일사용량_톤': [26., 66.]})
        score = metric.score_predictions(rows, np.array([20., 60.]), calibration, np.array([20., 60.]))['deviation_score']
        self.assertGreater(score.iloc[0], 2.5 * score.iloc[1])   # the same 6 tons is a larger share of a 20-ton level

    def test_ordinary_days_do_not_depend_on_the_model(self):
        usage = np.full(40, 30.)
        usage[20] = 90.
        frame = pd.DataFrame({'고객번호': ['1'] * 40, '날짜': pd.date_range('2026-01-01', periods=40), '일사용량_톤': usage})
        ordinary = metric.ordinary_days(frame)
        self.assertEqual(ordinary.tolist(), [day != 20 for day in range(40)])
        scored = metric.classify(metric.score_predictions(frame, np.full(40, 30.)), 2, 3)
        summary = metric.summarize(scored, ordinary)
        self.assertEqual((summary['n_ordinary_days'], summary['mae_ordinary_days'], scored.loc[20, '심각도']), (39, 0.0, '경고'))

    def test_pipeline_runs_end_to_end_on_a_small_dataset(self):
        rng = np.random.default_rng(0)
        days = pd.date_range('2025-01-01', periods=160)
        frame = pd.DataFrame([{
            '고객번호': f'{meter:09d}', '날짜': day.strftime('%Y-%m-%d'), '총승객수': 1000. * meter,
            '일사용량_톤': float(max(0, round(10 * meter * (1.2 if day.weekday() < 5 else 0.8) + rng.normal(0, 2)))),
            '역명': f'역{meter}', '사업소명': '사업소', '고지서_성명': f'역{meter}'} for meter in range(1, 9) for day in days])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            parts = {'train': frame['날짜'] <= '2025-04-30', 'test': frame['날짜'] > '2025-05-20'}
            parts['valid'] = ~parts['train'] & ~parts['test']
            frame.loc[frame['날짜'] >= '2025-06-01', '총승객수'] = np.nan   # ridership not collected yet for the newest days
            for name, rows in parts.items():
                frame[rows].to_csv(tmp / f'{name}.csv', index=False)
            build_calendar(frame, tmp / 'calendar.csv')
            pd.DataFrame(columns=['고객번호', '포함월수', '격월사용량_톤', '시작연월', '종료연월']).to_csv(tmp / 'bills.csv', index=False)
            cfg = yaml.safe_load((ROOT / 'back' / 'ml' / 'lightgbm' / 'config.yaml').read_text(encoding='utf-8'))
            cfg['data'] = {name: str(tmp / f'{name}.csv') for name in ('train', 'valid', 'test', 'calendar', 'bills')}
            cfg['model'] |= {'n_estimators': 20, 'n_jobs': 1}
            cfg['save_plot'] = False
            (tmp / 'config.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                metrics = run(tmp / 'config.yaml', tmp / 'out')
            anomalies = pd.read_csv(tmp / 'out' / 'test_anomalies.csv', encoding='utf-8-sig')
            self.assertEqual(len(anomalies), metrics['test']['n_samples'])
            self.assertTrue(np.isfinite(anomalies[['predicted_ton', 'deviation_score']].to_numpy()).all())
            self.assertLess(metrics['test']['mae'], 0.5 * anomalies['일사용량_톤'].mean())
            # days without ridership are judged by the spare model, and judged no worse than roughly the rest
            spare = anomalies['날짜'] >= '2025-06-01'
            self.assertTrue(anomalies.loc[spare, 'without_ridership'].all() and not anomalies.loc[~spare, 'without_ridership'].any())
            self.assertEqual(metrics['test_rows_without_ridership'], int(spare.sum()))
            self.assertLess((anomalies.loc[spare, '일사용량_톤'] - anomalies.loc[spare, 'predicted_ton']).abs().mean(), 0.5 * anomalies['일사용량_톤'].mean())
            for name in ('model.txt', 'model_without_ridership.txt'):
                self.assertTrue((tmp / 'out' / name).read_text().startswith('tree'))

    def test_bill_baseline_preserves_missing_usage_and_counts_distinct_months(self):
        labels = pd.DataFrame([{'고객번호': '000000001', '역명': '가역'}])
        common = {'고객번호': '000000001', '역명': '가역', '영업사업소': '동부', '용도': '직원', '부과금액_원': 100}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bills.csv'
            rows = [common | {'납기일': '2026-01-15', '총사용량_톤': 10},
                    common | {'납기일': '2026-01-31', '총사용량_톤': None}]
            pd.DataFrame(rows).to_csv(path, index=False)
            result = build_bills_bimonthly(path, labels)
            self.assertEqual(result.iloc[0]['포함월수'], 1)
            self.assertTrue(pd.isna(result.iloc[0]['격월사용량_톤']))
            self.assertEqual(result.iloc[0]['격월부과금액_원'], 200)
            rows.append(common | {'납기일': '2026-02-28', '총사용량_톤': 0})
            pd.DataFrame(rows).to_csv(path, index=False)
            result = build_bills_bimonthly(path, labels)
            self.assertEqual(result.iloc[0]['포함월수'], 2)
            self.assertTrue(pd.isna(result.iloc[0]['격월사용량_톤']))
            pd.DataFrame(rows).iloc[:0].to_csv(path, index=False)
            self.assertTrue(build_bills_bimonthly(path, labels).empty)


if __name__ == '__main__':
    unittest.main()
