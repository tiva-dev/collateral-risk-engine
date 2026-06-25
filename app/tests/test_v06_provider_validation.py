from __future__ import annotations
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from app.historical_data.models import HistoricalFXSeries, HistoricalSeries
from app.historical_data.models import HistoricalBar, HistoricalFXRate
from datetime import date, datetime, timezone
from app.simulations.data_builder import OfficialDatasetBuilder
from app.simulations.evidence_quality import scenario_eligibility, validate_evidence_package, validate_provider_coverage
from app.simulations.calibration import generate_calibration_diagnostics

class V06ProviderValidationTests(unittest.TestCase):
    def test_manual_workflow_dispatch_only(self):
        text=Path('.github/workflows/official-validation-provider-run.yml').read_text()
        self.assertIn('workflow_dispatch:', text)
        self.assertNotIn('pull_request:', text)
        self.assertNotIn('push:', text)
        self.assertIn('secrets.NGNMARKET_API_KEY', text)

    def test_workflow_creates_log_directory_before_tee(self):
        text=Path('.github/workflows/official-validation-provider-run.yml').read_text()
        mkdir_index=text.index('mkdir -p data/simulation_results')
        tee_index=text.index('tee data/simulation_results/dataset_build.log')
        self.assertLess(mkdir_index, tee_index)

    def test_planned_call_count_and_budget(self):
        b=OfficialDatasetBuilder(['ngnmarket'])
        counts=b.estimate_call_counts()
        self.assertGreater(counts['ngnmarket'], 0)
        with patch.dict('os.environ', {'NGNMARKET_MAX_CALLS_PER_RUN':'1'}):
            with self.assertRaises(RuntimeError): b.enforce_call_budgets(False)
            self.assertIn('ngnmarket', b.enforce_call_budgets(True))

    def test_coverage_and_eligibility(self):
        manifest={'missing_symbols':['SPY'], 'earliest_available_date_by_symbol': {'QQQ':'2018-01-01'}, 'fx_pairs':['USD/NGN']}
        cov=validate_provider_coverage(manifest, ['SPY','QQQ'], ['USD/NGN'])
        self.assertFalse(cov['passed'])
        self.assertIn('SPY', cov['missing_symbols'])
        elig=scenario_eligibility(manifest, ['us_diversified_etf_portfolio'])
        self.assertEqual(elig['eligible_scenarios'], [])
        self.assertTrue(elig['ineligible_scenarios'])

    def test_evidence_qa_and_calibration(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)
            metrics=[{'scenario':'s','base_scenario':'s','stress_name':'baseline','fx_missing_events':0,'total_interest_accrued':1,'provider_coverage_by_symbol':{},'dynamic_engine_versus_static_ltv_outcome_table':[], 'collateral_shortfall_rate':0, 'worst_shortfall':0, 'margin_call_frequency':0, 'liquidation_frequency':0, 'average_loan_balance':10, 'average_credit_capacity_preserved':8}]
            for name in ['official_validation_manifest.json','official_validation_metrics.csv','official_validation_report.md','provider_coverage_report.md','data_methodology.md','interest_accrual_methodology.md','simulation_assumptions.md']:
                (out/name).write_text('x')
            (out/'official_validation_metrics.json').write_text(json.dumps(metrics))
            self.assertTrue(validate_evidence_package({p.name:str(p) for p in out.iterdir()})['passed'])
            diag=generate_calibration_diagnostics(metrics, d)
            self.assertIn('s', diag['scenarios'])
            self.assertTrue((out/'calibration_diagnostics.json').exists())

    def test_smoke_refuses_without_confirmation(self):
        r=subprocess.run([sys.executable,'-m','app.simulations.run_provider_validation_smoke'],capture_output=True,text=True,check=True)
        self.assertIn('Refusing real provider calls', r.stdout)

    def test_smoke_with_mocked_providers(self):
        from app.simulations import run_provider_validation_smoke as smoke
        bar=HistoricalSeries('SPY',[HistoricalBar('SPY',date.today(),1,1,1,1)],'mock',datetime.now(timezone.utc),date.today(),date.today())
        fx=HistoricalFXSeries('EUR','USD',[HistoricalFXRate('EUR','USD',1.1,date.today())],'mock',datetime.now(timezone.utc),date.today(),date.today())
        with patch('app.simulations.run_provider_validation_smoke.AlpacaTradingHistoricalProvider') as A, patch('app.simulations.run_provider_validation_smoke.AlphaVantageHistoricalProvider') as V, patch('app.simulations.run_provider_validation_smoke.NGNMarketHistoricalProvider') as N:
            A.return_value.fetch_equity_history.return_value=bar
            V.return_value.fetch_fx_history.return_value=fx
            N.return_value.fetch_company_list.return_value=[{'symbol':'MTNN'}]
            self.assertEqual(smoke.main(['--confirm-real-provider-calls']), 0)

    def test_official_validation_cli_dry_run_supports_qa_calibration(self):
        r=subprocess.run([sys.executable,'-m','app.simulations.run_official_validation','--dry-run','--qa','--calibration','--allow-synthetic'],capture_output=True,text=True,check=True)
        self.assertIn('scenario_eligibility', r.stdout)

if __name__ == '__main__':
    unittest.main()
