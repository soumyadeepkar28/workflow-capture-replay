import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { validateWorkflowDefinition } from "../src/validate";

interface FixtureChange {
  path: Array<string | number>;
  value: unknown;
}

interface FixtureCase {
  name: string;
  expected_valid: boolean;
  typescript_error?: string;
  changes: FixtureChange[];
}

function loadJson(relativePath: string): unknown {
  return JSON.parse(
    readFileSync(new URL(relativePath, import.meta.url), { encoding: "utf-8" }),
  ) as unknown;
}

function applyChanges(base: unknown, changes: FixtureChange[]): unknown {
  const result = structuredClone(base);
  for (const change of changes) {
    let cursor = result as Record<string | number, unknown>;
    for (const segment of change.path.slice(0, -1)) {
      cursor = cursor[segment] as Record<string | number, unknown>;
    }
    const finalSegment = change.path.at(-1);
    if (finalSegment === undefined) {
      throw new Error("fixture change path cannot be empty");
    }
    cursor[finalSegment] = change.value;
  }
  return result;
}

const base = loadJson("../../protocol/fixtures/workflow_base.json");
const fixtureDocument = loadJson("../../protocol/fixtures/workflow_cases.json") as {
  cases: FixtureCase[];
};

describe("workflow contract conformance", () => {
  for (const fixtureCase of fixtureDocument.cases) {
    it(fixtureCase.name, () => {
      const result = validateWorkflowDefinition(applyChanges(base, fixtureCase.changes));

      expect(result.valid).toBe(fixtureCase.expected_valid);
      if (!fixtureCase.expected_valid) {
        expect(result.valid).toBe(false);
        if (!result.valid) {
          expect(result.errors.join("\n")).toContain(fixtureCase.typescript_error);
        }
      }
    });
  }
});