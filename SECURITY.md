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

The selected confidential reporting mechanism is [GitHub Private Vulnerability
Reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).
Once SOVTrace is public and this feature has been enabled, use the repository's
**Security / Report a vulnerability** interface to provide confidential details
and reproduction information.

This mechanism is not currently activated as a public reporting facility while the
repository remains private. Adding this policy does not enable it. If the reporting
interface is unavailable, do not disclose sensitive vulnerability details in
public issues, discussions or pull requests. A non-sensitive request for a
confidential reporting route through ordinary project contact mechanisms is
appropriate.

No acknowledgement or remediation deadline is promised.

## Required P4 activation and verification

The following are mandatory downstream P4 release dependencies, not LQ-05 actions:

1. Complete all pre-publication reviews and obtain explicit owner approval of the
   exact intended public commit.
2. Only under that approval, change repository visibility to public.
3. Enable GitHub Private Vulnerability Reporting in the repository's security
   settings.
4. Verify that the public repository offers the expected private reporting
   interface and that this policy accurately describes it.
5. Require successful verification before release tagging or package distribution.

If the facility cannot be enabled or verified, stop further release actions and
escalate for an owner decision. Do not substitute public issue reporting, invent an
alternative contact address or assume this requirement has passed. P4 remains
blocked until its independently reviewed release conditions and this
security-reporting requirement have been satisfied.
