---
name: spec-gen
description: Generate structured SPEC documents from PRDs. Parses PRD markdown, extracts requirements, acceptance criteria, and technical approach, then generates a GEARS-formatted SPEC document saved to .harness/specs/.
---

# /laplace:spec-gen

## Intent

Generate a structured SPEC document from a PRD (Product Requirements Document) markdown file. The SPEC follows the GEARS (Given/When/Then/And/Result) format and includes sections for requirements, acceptance criteria, technical approach, implementation plan, and risk assessment.

## When to Run

- After creating or updating a PRD
- Before starting implementation planning
- When you need a structured specification from requirements
- To standardize document format for the workflow

## What It Does

### Step 1: Parse PRD

Read the PRD markdown file and extract:
- Frontmatter metadata (title, type, priority)
- Overview and description
- Requirements (functional and non-functional)
- Acceptance criteria
- Technical notes and architecture
- Risk assessment

### Step 2: Generate SPEC Structure

Create a SPEC document with the following sections:

```markdown
---
spec_id: SPEC-YYYYMMDD-HHMMSS
status: draft
created_at: YYYY-MM-DDTHH:MM:SSZ
type: feature
priority: high
---

# Title

Description

## Overview
Purpose and scope

## Requirements
### Functional Requirements
- FR-1: Requirement text

### Non-Functional Requirements
- NFR-1: Performance requirement

## Acceptance Criteria
### AC-1: Given...When...Then...
- Given condition
- When action
- Then result

## Technical Approach
### Technical Considerations
Architecture decisions

### Components
Component list

## Implementation Plan
### Phase 1: Planning
### Phase 2: Implementation
### Phase 3: Validation

## Risk Assessment
### Identified Risks
Risk description

### Mitigation Strategies
Mitigation approach
```

### Step 3: Save SPEC

Save the generated SPEC to `.harness/specs/SPEC-{id}.md`

## Output Format

```
Laplace result: SPEC generated

SPEC: SPEC-YYYYMMDD-HHMMSS
File: .harness/specs/SPEC-YYYYMMDD-HHMMSS.md
Title: <PRD title>

Sections:
- Overview: ✓
- Requirements: ✓
- Acceptance Criteria: ✓
- Technical Approach: ✓
- Implementation Plan: ✓
- Risk Assessment: ✓

Next: Review SPEC and proceed to /laplace:workflow-gen
```

## Constraints

- PRD must be valid markdown
- Frontmatter is optional but recommended
- Acceptance criteria should follow GEARS format
- Technical notes are optional (auto-generated if missing)

## Example

Input PRD (`my-feature.md`):

```markdown
---
title: User Authentication
type: feature
priority: high
---

# User Authentication

## Overview
Implement OAuth2 login.

## Requirements
1. Support Google OAuth
2. Support GitHub OAuth

## Acceptance Criteria
### AC-1: Google Login
Given a user on the login page
When they click "Sign in with Google"
Then they are redirected to Google OAuth

## Technical Notes
Using OAuth2 proxy for authentication.
```

Output SPEC:

```markdown
---
spec_id: SPEC-20250128-120000
status: draft
created_at: 2025-01-28T12:00:00Z
type: feature
priority: high
---

# User Authentication

## Overview
Implement OAuth2 login.

**Purpose:** Enable user authentication via OAuth providers

## Requirements
### Functional Requirements
- FR-1: Support Google OAuth
- FR-2: Support GitHub OAuth

### Non-Functional Requirements
No non-functional requirements specified.

## Acceptance Criteria
### AC-1: Given...When...Then...
  - Given a user on the login page
  - When they click "Sign in with Google"
  - Then they are redirected to Google OAuth

## Technical Approach
### Technical Considerations
Using OAuth2 proxy for authentication.

## Implementation Plan
### Phase 1: Planning
- Review and finalize requirements
- Create detailed design documents
- Define testing strategy

### Phase 2: Implementation
- Implement core functionality
- Write unit tests
- Conduct code reviews

### Phase 3: Validation
- Execute test suite
- Verify acceptance criteria
- Performance testing (if applicable)

## Risk Assessment
### Risk Categories
- **Technical Risk**: Implementation complexity
- **Integration Risk**: Compatibility with existing systems
- **Performance Risk**: Impact on system performance
- **Security Risk**: Potential security vulnerabilities
```
