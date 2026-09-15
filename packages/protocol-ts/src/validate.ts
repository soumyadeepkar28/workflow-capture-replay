import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import workflowSchema from "../../protocol/schema/workflow-definition.schema.json";
import type { WorkflowDefinition } from "./generated";

export type WorkflowValidationResult =
  | { valid: true; value: WorkflowDefinition }
  | { valid: false; errors: string[] };

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validateSchema = ajv.compile<WorkflowDefinition>(workflowSchema);

function pathIsNormalized(path: string): boolean {
  return !path.includes("//") && !path.split("/").some((segment) => segment === "." || segment === "..");
}

function semanticErrors(workflow: WorkflowDefinition): string[] {
  const errors: string[] = [];
  const actionIds = new Set<string>();

  workflow.actions.forEach((action, index) => {
    if (action.sequence !== index) {
      errors.push("action sequences must be contiguous and start at zero");
    }
    if (actionIds.has(action.action_id)) {
      errors.push("action IDs must be unique within a workflow");
    }
    actionIds.add(action.action_id);

    const pages = action.kind === "navigate" ? [action.destination] : [action.page];
    if (action.kind === "click" && action.expected_page_after) {
      pages.push(action.expected_page_after);
    }
    for (const page of pages) {
      if (!pathIsNormalized(page.path)) {
        errors.push("path must be a normalized relative target path");
      }
    }

    if ("locator" in action && action.locator.scope) {
      const { element_id: elementId, name } = action.locator.scope;
      if (!elementId && !name) {
        errors.push("locator scope needs an accessible name or element ID");
      }
    }
  });

  return [...new Set(errors)];
}

export function validateWorkflowDefinition(input: unknown): WorkflowValidationResult {
  if (!validateSchema(input)) {
    return {
      valid: false,
      errors: (validateSchema.errors ?? []).map(
        (error) => `${error.instancePath || "/"} ${error.message ?? error.keyword}`,
      ),
    };
  }

  const errors = semanticErrors(input);
  return errors.length === 0 ? { valid: true, value: input } : { valid: false, errors };
}