"""Model and texture artifacts: reading them, and converting between them.

The tools that produce these artifacts are structurally unable to report
failure -- three separate broken outcomes were measured returning success --
so everything here reads the artifact rather than the tool's report.
"""
