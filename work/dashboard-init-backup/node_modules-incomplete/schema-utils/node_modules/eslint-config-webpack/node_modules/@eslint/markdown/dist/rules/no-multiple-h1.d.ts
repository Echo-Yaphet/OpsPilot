declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let multipleH1: string;
        }
        let schema: {
            type: "object";
            properties: {
                frontmatterTitle: {
                    type: "string";
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            frontmatterTitle: string;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: NoMultipleH1Options;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "multipleH1";
    }>): {
        "yaml, toml, json"(node: Yaml | Toml | Json): void;
        html(node: import("mdast").Html): void;
        heading(node: import("mdast").Heading): void;
    };
}
export default _default;
export type NoMultipleH1MessageIds = "multipleH1";
export type NoMultipleH1Options = [{
    frontmatterTitle?: string;
}];
export type NoMultipleH1RuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoMultipleH1Options;
    MessageIds: NoMultipleH1MessageIds;
}>;
import type { Yaml } from "mdast";
import type { Toml } from "../types.js";
import type { Json } from "../types.js";
import type { MarkdownRuleDefinition } from "../types.js";
