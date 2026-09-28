# Cobalt Harbor Bank — Customer Data Handling and Privacy Policy

> **MOCK DOCUMENT.** Cobalt Harbor Bank is a fictional institution. This policy was written for the Secure Enterprise AI Evaluation Sandbox and contains no real customer data. It is not legal or compliance advice.

- **Policy ID:** CHB-POL-017
- **Version:** 3.2
- **Owner:** Office of the Chief Compliance Officer
- **Approved by:** Board Risk Committee
- **Review cycle:** Annual, or sooner after a material regulatory change

## 1. Purpose and Scope

This policy defines how Cobalt Harbor Bank ("the Bank") collects, classifies, stores, shares, retains, and disposes of customer information. It applies to all employees, contractors, consultants, and third-party service providers who access customer information in any form, including paper records, core banking systems, data warehouses, email, and artificial intelligence (AI) tools.

The policy supports the Bank's obligations under the Gramm-Leach-Bliley Act (GLBA) Safeguards Rule, the Bank Secrecy Act (BSA), the Payment Card Industry Data Security Standard (PCI DSS), and applicable state privacy laws. Where this policy and a law conflict, the stricter requirement applies.

## 2. Data Classification

All customer information must be assigned one of four classification levels. The data owner assigns the level; when in doubt, the higher level applies.

**Restricted.** Information whose disclosure could cause severe harm to customers or the Bank. Includes Social Security numbers, taxpayer identification numbers, full payment card numbers, card verification values, online banking credentials, full account numbers paired with routing numbers, and biometric data. Restricted data must be encrypted at rest and in transit, may be accessed only by named roles, and must never be entered into any AI tool unless it has first passed through an approved masking control.

**Confidential.** Non-public personal information that is not Restricted, such as customer names combined with account balances, transaction histories, loan applications, credit decisions, and contact details. Confidential data must be encrypted in transit and stored only in approved systems.

**Internal.** Bank information not intended for the public that does not identify a customer, such as aggregate portfolio statistics, internal procedures, and this policy.

**Public.** Information approved for public release, such as published rates, branch hours, and marketing materials.

## 3. Collection and Minimization

Employees may collect only the customer information needed for a documented business purpose. Forms, systems, and AI prompts must not request data fields that are not required for that purpose. Free-text fields in case-management systems must not be used to record Restricted data; staff should reference the secure record instead.

Customer identification under the Customer Identification Program (CIP) must be collected at account opening and verified within 30 calendar days. Accounts that cannot be verified within that window must be escalated to the BSA Officer for a decision on restriction or closure.

## 4. Access Control

Access to customer information follows the principle of least privilege. Access is granted by role, approved by the employee's manager and the data owner, and provisioned through the Identity and Access Management team.

- Access to Restricted data requires multi-factor authentication for every session.
- Managers must recertify their team's access to Confidential and Restricted systems every 90 days.
- Access for departing employees must be revoked no later than the end of their last working day. Access for employees terminated involuntarily must be revoked immediately, before the employee is notified where practical.
- Shared or generic accounts are prohibited for systems containing Confidential or Restricted data.
- Privileged administrator activity on core banking systems must be logged, and those logs must be reviewed weekly by Information Security.

## 5. Encryption and Storage

Restricted and Confidential data must be encrypted in transit using TLS 1.2 or higher. Restricted data must also be encrypted at rest using AES-256 or an equivalent algorithm approved by the Chief Information Security Officer (CISO). Encryption keys are managed in the Bank's hardware security modules and rotated at least every 12 months.

Customer information may not be stored on removable media, personal devices, or personal cloud storage accounts. Laptops that access customer information must use full-disk encryption.

## 6. Retention and Disposal

Records must be retained for the periods below, measured from the event stated. When a legal hold is in place, the hold overrides the schedule until Legal releases it.

| Record type | Retention period |
|---|---|
| Customer identification (CIP/KYC) records | 5 years after the account is closed |
| Wire transfer records | 5 years after the transfer date |
| Currency Transaction Reports (CTRs) and supporting documents | 5 years after filing |
| Suspicious Activity Reports (SARs) and supporting documents | 5 years after filing |
| Loan files, closed or paid off | 7 years after final payment |
| Customer complaint records | 3 years after resolution |
| Recorded phone calls with customers | 2 years after the call |
| Marketing consent and opt-out records | 5 years after the consent is withdrawn |
| AI assistant prompt and response logs | 1 year after creation |

At the end of the retention period, records must be disposed of securely. Paper records are cross-cut shredded through the Bank's approved vendor. Electronic records are deleted using methods approved by Information Security, and storage media are destroyed or cryptographically wiped before reuse or disposal. Disposal of Restricted records must be logged.

## 7. Sharing with Third Parties

Customer information may be shared with a third-party service provider only when:

1. Vendor Risk Management has completed a due-diligence review within the last 12 months;
2. a written contract requires the provider to protect the information to a standard at least as strict as this policy, and to notify the Bank of any security incident within 24 hours of discovery; and
3. only the minimum data needed for the service is shared.

The Bank does not sell customer information. Sharing with non-affiliated third parties for marketing purposes requires that the customer received a privacy notice and did not opt out.

## 8. Payments and Wire Transfer Controls

Outgoing wire transfers are subject to the following verification controls:

- Wire transfers of $10,000 or more initiated by phone, email, or fax require a callback to the customer at a phone number already on file. The callback must not use any phone number provided in the request itself.
- Wire transfers of $250,000 or more require approval by two authorized officers (dual control), regardless of channel.
- Any request to change a customer's wire instructions or beneficiary bank details must be verified by callback and held for 24 hours before the first transfer to the new instructions.
- International wires to jurisdictions on the Bank's high-risk country list must be reviewed by the BSA/AML team before release.

Cash transactions over $10,000 in a single business day require a Currency Transaction Report, which must be filed within 15 calendar days of the transaction.

## 9. Payment Card Data

Full payment card numbers (primary account numbers) may be stored only in the Bank's PCI-scoped card platform. Everywhere else, card numbers must be truncated to show no more than the first six and last four digits. Card verification values (CVV/CVC) and PIN data must never be stored after authorization, in any system, under any circumstances.

## 10. Use of Artificial Intelligence Tools

The Bank permits AI tools only when they have been approved by the Model Risk Management Committee and meet the controls in this section.

- **Approved tools only.** Employees may not paste customer information into public or consumer AI services. Only AI tools on the Bank's approved-tools register may be used with Internal, Confidential, or Restricted data.
- **Data residency.** Approved AI tools that process Confidential or Restricted data must run on Bank-controlled infrastructure or in a private environment where the provider contractually agrees not to retain the data or use it for model training.
- **Masking before processing.** Restricted data must be detected and masked before it reaches an AI model, a vector database, or any log file. The masking control must be tested at least quarterly, and its detection recall must be measured and reported to the Model Risk Management Committee.
- **Human review.** AI output must not be used as the sole basis for a credit decision, account closure, SAR filing decision, or any communication that changes a customer's rights. A qualified employee must review and approve the output first.
- **Logging.** Prompts and responses are logged after masking and retained for 1 year for audit purposes. Logs must never contain unmasked Restricted data.
- **Tenant separation.** When one AI deployment serves multiple business lines or client institutions, each tenant's documents must be isolated so that a query from one tenant cannot retrieve another tenant's data. Isolation must be enforced by the data store, not only by application code, and tested before go-live.
- **Performance monitoring.** The business owner of each AI tool must track response latency and accuracy, and must escalate to Model Risk Management if the 95th-percentile response time exceeds the service-level target for two consecutive weeks.

## 11. Incident Response and Breach Notification

Any employee who suspects that customer information has been lost, stolen, accessed without authorization, or disclosed improperly must report it to the Information Security hotline within 1 hour of discovery. Employees must not attempt to investigate the incident themselves.

The Incident Response Team will:

1. contain the incident and preserve evidence;
2. notify the CISO and the Chief Compliance Officer within 4 hours of the report if customer information is involved;
3. determine, with Legal, whether regulator notification is required. A notification incident under the federal computer-security incident notification rule must be reported to the Bank's primary federal regulator as soon as possible and no later than 36 hours after the Bank determines that the incident occurred; and
4. notify affected customers without unreasonable delay, and in any case within the deadlines set by applicable state law.

A post-incident review must be completed within 30 days of containment, with remediation actions tracked to closure.

## 12. Training

All employees must complete privacy and information security training within 30 days of hire and annually thereafter. Employees with access to Restricted data, and employees who use approved AI tools, must complete additional role-specific training annually. Access to Restricted systems is suspended for employees whose training is more than 14 days overdue.

## 13. Exceptions and Enforcement

Exceptions to this policy must be requested in writing, approved by the CISO and the Chief Compliance Officer, recorded in the exceptions register, and reviewed at least every 6 months. No exception may be granted for the storage of card verification values or PIN data.

Violations of this policy may result in disciplinary action up to and including termination of employment or contract, and may be reported to law enforcement or regulators where required.
