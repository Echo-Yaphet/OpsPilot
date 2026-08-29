declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let emptyDefinition: string;
            let emptyFootnoteDefinition: string;
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
        RuleOptions: NoEmptyDefinitionsOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: NoEmptyDefinitionsMessageIds;
    }>): {
        definition(node: import("mdast").Definition): void;
        footnoteDefinition(node: import("mdast").FootnoteDefinition): void;
    };
}
export default _default;
export type NoEmptyDefinitionsMessageIds = "emptyDefinition" | "emptyFootnoteDefinition";
export type NoEmptyDefinitionsOptions = [{
    allowDefinitions?: string[];
    allowFootnoteDefinitions?: string[];
    checkFootnoteDefinitions?: boolean;
}];
export type NoEmptyDefinitionsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoEmptyDefinitionsOptions;
    MessageIds: NoEmptyDefinitionsMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
