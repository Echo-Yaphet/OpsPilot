declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let missingLanguage: string;
            let disallowedLanguage: string;
        }
        let schema: {
            type: "object";
            properties: {
                required: {
                    type: "array";
                    items: {
                        type: "string";
                    };
                    uniqueItems: true;
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            required: any[];
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: FencedCodeLanguageOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: FencedCodeLanguageMessageIds;
    }>): {
        code(node: import("mdast").Code): void;
    };
}
export default _default;
export type FencedCodeLanguageMessageIds = "missingLanguage" | "disallowedLanguage";
export type FencedCodeLanguageOptions = [{
    required?: string[];
}];
export type FencedCodeLanguageRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: FencedCodeLanguageOptions;
    MessageIds: FencedCodeLanguageMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
