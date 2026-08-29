declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        let schema: {
            type: "object";
            properties: {
                ignoreCase: {
                    type: "boolean";
                };
                allowPattern: {
                    type: "string";
                };
            };
            additionalProperties: false;
        }[];
        namespace messages {
            let invalidFragment: string;
        }
        let defaultOptions: [{
            ignoreCase: true;
            allowPattern: string;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: NoMissingLinkFragmentsOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "invalidFragment";
    }>): {
        heading(): void;
        "heading *:not(html)"({ value }: any): void;
        "heading:exit"(): void;
        html(node: import("mdast").Html): void;
        "definition, link"(node: Definition | Link): void;
        "root:exit"(): void;
    };
}
export default _default;
export type NoMissingLinkFragmentsMessageIds = "invalidFragment";
export type NoMissingLinkFragmentsOptions = [{
    ignoreCase?: boolean;
    allowPattern?: string;
}];
export type NoMissingLinkFragmentsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoMissingLinkFragmentsOptions;
    MessageIds: NoMissingLinkFragmentsMessageIds;
}>;
import type { Definition } from "mdast";
import type { Link } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
