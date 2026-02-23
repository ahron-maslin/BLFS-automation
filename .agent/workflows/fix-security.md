---
description: get dependabot alerts and fix them
---

# Workflow: Fix Dependabot Alerts
# Description: Automatically scans GitHub for security alerts and fixes them.

## Step 1: Reconnaissance
- **Action:** Open the integrated browser to `https://github.com/ahron-maslin/BLFS-automation/security/dependabot`.
- **Goal:** Identify the top 3 "High" or "Critical" alerts.
- **Output:** List the package name, current version, and the "fixed" version recommended by GitHub.

## Step 2: The Plan
- **Action:** Create an **Implementation Plan** artifact.
- **Goal:** Detail how to update the `package.json` (or `requirements.txt`) and which terminal commands to run (e.g., `npm install`).

## Step 3: Execution
- **Action:** Update the dependency files in the Editor.
- **Action:** Run the install command in the Terminal.
- **Action:** Run the project's test suite (e.g., `npm test`).

## Step 4: Verification
- **Goal:** If tests fail, attempt 2 rounds of automated debugging.
- **Action:** Provide a final **Walkthrough** showing the "Before" and "After" versions.