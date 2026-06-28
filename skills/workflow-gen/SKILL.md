---
name: workflow-gen
description: Generate executable workflow plans from SPEC documents. Parses SPEC, extracts requirements and acceptance criteria, then generates step-by-step workflow with evidence points and quality gates.
---

# /laplace:workflow-gen

## Intent

Generate an executable workflow plan from a SPEC document. The workflow includes step-by-step implementation plan, evidence collection points, and quality gates for validation.

## When to Run

- After generating a SPEC with `/laplace:spec-gen`
- Before starting implementation
- When you need a detailed execution plan
- To standardize workflow for team execution

## What It Does

### Step 1: Parse SPEC

Read the SPEC markdown file and extract:
- Frontmatter metadata (spec_id, status, type)
- Requirements (functional and non-functional)
- Acceptance criteria (AC sections)
- Technical approach and components
- Risk assessment

### Step 2: Generate Workflow Steps

Create workflow steps automatically:

1. **Analysis Step** (PM Agent)
   - Review SPEC requirements
   - Validate acceptance criteria
   - Evidence: `spec-validation`

2. **Design Step** (Architect Agent)
   - Create technical design
   - Define component structure
   - Evidence: `workflow-plan`

3. **Implementation Steps** (Dev Agent)
   - One step per acceptance criteria
   - Implement and test each AC
   - Evidence: `test`, `review`

4. **Testing Step** (QA Agent)
   - Execute full test suite
   - Integration testing
   - Evidence: `test`, `integration-test`

5. **Review Step** (Reviewer Agent)
   - Code review against AC
   - Verify implementation completeness
   - Evidence: `review`

6. **Security Review Step** (Security Agent) - if security-sensitive
   - Security-focused review
   - Vulnerability assessment
   - Evidence: `security`

### Step 3: Define Quality Gates

Add quality gates at critical points:

- **Plan Audit Gate** (after Design → before Implementation)
  - Required: `spec-validation`, `workflow-plan`
  - Auditor: `plan-auditor`

- **Sync Audit Gate** (after Review → before Complete)
  - Required: `test`, `review`, `audit-report`
  - Auditor: `sync-auditor`

### Step 4: Save Workflow

Save the generated workflow to `.harness/workflows/PLAN-{id}.json` and `.harness/workflows/PLAN-{id}.md`

## Output Format

```
Laplace result: Workflow plan generated

Plan: PLAN-YYYYMMDD-HHMMSS
File: .harness/workflows/PLAN-YYYYMMDD-HHMMSS.md
Steps: 6
Gates: 2

Workflow Steps:
1. Analyze Requirements (pm)
2. Create Design (architect)
3. Implement AC-1 (dev)
4. Implement AC-2 (dev)
5. Execute Tests (qa)
6. Review Implementation (reviewer)

Quality Gates:
- Gate-1: Plan Audit (step-2 → step-3)
- Gate-2: Sync Audit (step-6 → complete)

Next: Review workflow and proceed to /laplace:auto-run
```

## Example

Input SPEC (`SPEC-20250128-120000.md`):

```markdown
---
spec_id: SPEC-20250128-120000
status: draft
type: feature
priority: high
---

# User Authentication

## Acceptance Criteria

### AC-1: Google Login
- Given a user on login page
- When they click "Sign in with Google"
- Then they are redirected to Google OAuth
```

Output Workflow:

```markdown
# User Authentication

**Plan ID:** PLAN-20250128-120500
**SPEC ID:** SPEC-20250128-120000
**Created:** 2025-01-28T12:05:00Z

## Workflow Steps

### 1. Analyze Requirements
**Step ID:** step-1
**Phase:** analyze
**Agent:** pm

Review SPEC requirements and acceptance criteria

**Evidence Required:** spec-validation

### 2. Create Design
**Step ID:** step-2
**Phase:** design
**Agent:** architect

Create technical design and component structure

**Dependencies:** step-1
**Evidence Required:** workflow-plan

### 3. Implement AC-1: Google Login
**Step ID:** step-3
**Phase:** implement
**Agent:** dev

Given a user on login page / When they click "Sign in with Google" / Then they are redirected to Google OAuth

**Dependencies:** step-2
**Evidence Required:** test, review
**AC:** AC-1

### 4. Execute Tests
**Step ID:** step-4
**Phase:** test
**Agent:** qa

Run test suite and capture evidence

**Dependencies:** step-3
**Evidence Required:** test, integration-test

### 5. Review Implementation
**Step ID:** step-5
**Phase:** review
**Agent:** reviewer

Code review against acceptance criteria

**Dependencies:** step-4
**Evidence Required:** review

## Quality Gates

### Gate-1
**From:** step-2 → **To:** step-3

Plan audit: Verify workflow plan is complete

**Required Evidence:** spec-validation, workflow-plan
**Auditor:** plan

### Gate-2
**From:** step-5 → **To:** complete

Sync audit: Verify implementation meets acceptance criteria

**Required Evidence:** test, review, audit-report
**Auditor:** sync
```

## Constraints

- SPEC must exist in `.harness/specs/`
- Acceptance criteria should follow GEARS format
- Security step added if SPEC has security_sensitive=true
- All steps depend on previous step (sequential by default)

## Integration

This skill integrates with:
- `/laplace:spec-gen`: Generates input SPEC
- `/laplace:auto-run`: Executes generated workflow
- `components/verify/`: Auditors for quality gates
- `scripts/loop_ledger.py`: Records workflow execution
