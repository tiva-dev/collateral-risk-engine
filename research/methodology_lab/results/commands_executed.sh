# Runs cited in the methodology document (25 Nov 2026). <main> = v0.6.2 checkout, <pr21> = v0.7.0 checkout.
A="--v062 <main> --v070 <pr21> --workers 2"
python -m research.methodology_lab.deterministic --v062 <main> --v070 <pr21> --out deterministic.json
# Policy 2: common maintenance standard (B uncapped capacity), call at 1.15x
python -m research.methodology_lab.run_experiments --out common.json $A --reps 8 --cohorts 50 --c-every 5 --trigger common --psi 1.15
# Policy 1: each method's own maintenance standard, with B_gap
python -m research.methodology_lab.run_experiments --out own.json $A --reps 5 --cohorts 50 --c-every 5 --trigger own --psi 1.15 --with-bgap
# Sensitivities (policy 1)
python -m research.methodology_lab.run_experiments --out sens_grace5.json $A --reps 3 --cohorts 40 --c-every 1000 --trigger own --psi 1.15 --grace 5 --with-bgap --appetites conservative
python -m research.methodology_lab.run_experiments --out sens_psi105.json $A --reps 3 --cohorts 40 --c-every 1000 --trigger own --psi 1.05 --with-bgap --appetites conservative
python -m research.methodology_lab.run_experiments --out sens_psi130.json $A --reps 3 --cohorts 40 --c-every 1000 --trigger own --psi 1.30 --with-bgap --appetites conservative
# own_subset_baseline.csv = own.json restricted to rep < 3 and cohort < 40 (matches the sensitivity sample)
