declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let missingMetadata: string;
            let disallowedMetadata: string;
        }
        let schema: {
            enum: string[];
        }[];
        let defaultOptions: ["always"];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: FencedCodeMetaOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: FencedCodeMetaMessageIds;
    }>): {
        code(node: import("mdast").Code): void;
    };
}
export default _default;
export type FencedCodeMetaMessageIds = "missingMetadata" | "disallowedMetadata";
export type FencedCodeMetaOptions = ["always" | "never"];
export type FencedCodeMetaRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: FencedCodeMetaOptions;
    MessageIds: FencedCodeMetaMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
