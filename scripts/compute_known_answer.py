#!/usr/bin/env python3
"""Source-checkout shim for the installed independent route (architecture.md section 5.6, D436).

The program itself ships as ``agent_bi/verify/compute_known_answer.py``. This file only calls
that module's ``main`` so the command line, output file and exit codes are identical. Checkout
scripts such as ``compute_known_answers_idle_warehouse_waste.sh`` call this shim; no runtime
Agent BI command uses it.
"""
import sys

from agent_bi.verify.compute_known_answer import main

if __name__ == "__main__":
    sys.exit(main())
