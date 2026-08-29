declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let skippedHeading: string;
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
        RuleOptions: HeadingIncrementOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "skippedHeading";
    }>): {
        yaml(node: import("mdast").Yaml): void;
        toml(node: import("../types.js").Toml): void;
        json(node: import("../types.js").Json): void;
        heading(node: import("mdast").Heading): void;
    };
}
export default _default;
export type HeadingIncrementMessageIds = "skippedHeading";
export type HeadingIncrementOptions = [{
    frontmatterTitle?: string;
}];
export type HeadingIncrementRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: HeadingIncrementOptions;
    MessageIds: HeadingIncrementMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
