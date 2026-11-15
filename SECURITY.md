# Security policy

SOVTrace is a local, bounded CLI. Security-relevant handling includes untrusted
`.xlsx` files and workbook resource limits, model-provider communications that may
contain schema metadata or user questions, restricted SQL generation and
validation, and local SQLite execution with retained source evidence.

The application has finite resource ceilings and fail-closed controls. These
controls do not guarantee the absence of vulnerabilities or comprehensive
protection against every resource-exhaustion technique. Version `0.1.0` is
currently unreleased; this policy does not define a supported-release matrix or
promise support for older versions.

## Reporting a vulnerability

SOVTrace accepts confidential vulnerability reports through
[GitHub Private Vulnerability Reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

Use this repository's **Security and quality / Report a vulnerability**
interface to submit vulnerability details and reproduction information
privately. GitHub may require you to sign in before submitting a report.

Do not disclose sensitive vulnerability information in public issues,
discussions or pull requests.

If the private reporting interface is unavailable, do not submit
vulnerability details publicly. A non-sensitive request for a confidential
reporting route through ordinary project contact mechanisms is appropriate.

No acknowledgement or remediation deadline is promised.
