declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let description: string;
            let url: string;
        }
        namespace messages {
            let disallowedElement: string;
        }
        let schema: {
            type: "object";
            properties: {
                allowed: {
                    type: "array";
                    items: {
                        type: "string";
                    };
                    uniqueItems: true;
                };
                allowedIgnoreCase: {
                    type: "boolean";
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            allowed: any[];
            allowedIgnoreCase: false;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: NoHtmlOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "disallowedElement";
    }>): {
        html(node: import("mdast").Html): void;
    };
}
export default _default;
export type NoHtmlMessageIds = "disallowedElement";
export type NoHtmlOptions = [{
    allowed?: string[];
    allowedIgnoreCase?: boolean;
}];
export type NoHtmlRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoHtmlOptions;
    MessageIds: NoHtmlMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
