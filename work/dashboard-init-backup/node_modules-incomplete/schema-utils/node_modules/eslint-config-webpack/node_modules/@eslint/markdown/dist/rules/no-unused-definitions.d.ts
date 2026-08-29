declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let unusedDefinition: string;
            let unusedFootnoteDefinition: string;
        }
        let schema: {
            type: "object";
            properties: {
                allowDefinitions: {
                    type: "array";
                    items: {
                        type: "string";
                    };
                    uniqueItems: true;
                };
                allowFootnoteDefinitions: {
                    type: "array";
                    items: {
                        type: "string";
                    };
                    uniqueItems: true;
                };
                checkFootnoteDefinitions: {
                    type: "boolean";
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            allowDefinitions: string[];
            allowFootnoteDefinitions: any[];
            checkFootnoteDefinitions: true;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: NoUnusedDefinitionsOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: NoUnusedDefinitionsMessageIds;
    }>): {
        imageReference(node: import("mdast").ImageReference): void;
        linkReference(node: import("mdast").LinkReference): void;
        footnoteReference(node: import("mdast").FootnoteReference): void;
        definition(node: Definition): void;
        footnoteDefinition(node: FootnoteDefinition): void;
        "root:exit"(): void;
    };
}
export default _default;
export type NoUnusedDefinitionsMessageIds = "unusedDefinition" | "unusedFootnoteDefinition";
export type NoUnusedDefinitionsOptions = [{
    allowDefinitions?: string[];
    allowFootnoteDefinitions?: string[];
    checkFootnoteDefinitions?: boolean;
}];
export type NoUnusedDefinitionsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoUnusedDefinitionsOptions;
    MessageIds: NoUnusedDefinitionsMessageIds;
}>;
import type { Definition } from "mdast";
import type { FootnoteDefinition } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
