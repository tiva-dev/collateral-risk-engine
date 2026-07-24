import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.historical_data.cache import HistoricalDataCache
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.simulations.calibration import generate_calibration_diagnostics
from app.simulations.run_official_validation import _load_replay_inputs
from app.simulations.run_provider_validation_smoke import _sanitize


class V061RealDataReadinessTests(unittest.TestCase):
    def test_alpha_vantage_fx_write_is_canonical_and_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            provider=AlphaVantageHistoricalProvider(HistoricalDataCache(td))
            response={'Time Series FX (Daily)': {'2024-01-02': {'4. close':'1.1'}}}
            with patch.object(provider, '_request_json', return_value=response):
                provider.fetch_fx_history('EUR','USD',date(2024,1,1),date(2024,1,3),force_refresh=True)
            payload=json.loads(Path(provider.cache_paths[0]).read_text())['data']
            self.assertEqual(payload['cache_schema'],'historical_fx_series/v1')
            self.assertIn('data_quality_summary',payload)
            bars,fx=_load_replay_inputs({'cache_paths':provider.cache_paths})
            self.assertFalse(bars); self.assertIn(('EUR','USD'),fx)

    def test_provider_native_cache_is_recorded_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'native.json'; path.write_text(json.dumps({'data':{'Time Series (Daily)':{}}}))
            manifest={'cache_paths':[str(path)]}
            with self.assertWarnsRegex(RuntimeWarning,'provider-native'):
                bars,fx=_load_replay_inputs(manifest)
            self.assertFalse(bars); self.assertFalse(fx); self.assertTrue(manifest['qa_cache_warnings'])

    def test_calibration_does_not_substitute_loan_balance(self):
        d=generate_calibration_diagnostics([{'scenario':'s','average_loan_balance':999,'average_approved_credit':100,'average_lifecycle_safe_credit_limit':80}])['scenarios']['s']
        self.assertEqual(d['average_approved_credit_by_scenario']['value'],100)
        missing=generate_calibration_diagnostics([{'scenario':'s','average_loan_balance':999}])['scenarios']['s']
        self.assertEqual(missing['average_approved_credit_by_scenario']['status'],'unavailable')

    def test_smoke_sanitizes_credentials(self):
        text=_sanitize('failed api_key=abc123 Authorization: Bearer token456')
        self.assertNotIn('abc123',text); self.assertNotIn('token456',text)

    def test_workflow_dry_run_validation_guardrail(self):
        text=Path('.github/workflows/official-validation-provider-run.yml').read_text()
        self.assertIn("inputs.dry_run == 'true' && inputs.run_validation_after_build == 'true'",text)
        self.assertIn('Cannot run official validation after a dry-run dataset build because no provider cache data is generated.',text)

if __name__ == '__main__': unittest.main()
