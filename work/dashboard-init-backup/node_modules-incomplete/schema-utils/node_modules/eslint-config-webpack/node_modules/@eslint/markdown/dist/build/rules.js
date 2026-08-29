import rule0 from "../rules/fenced-code-language.js";
import rule1 from "../rules/fenced-code-meta.js";
import rule2 from "../rules/heading-increment.js";
import rule3 from "../rules/no-bare-urls.js";
import rule4 from "../rules/no-duplicate-definitions.js";
import rule5 from "../rules/no-duplicate-headings.js";
import rule6 from "../rules/no-empty-definitions.js";
import rule7 from "../rules/no-empty-images.js";
import rule8 from "../rules/no-empty-links.js";
import rule9 from "../rules/no-html.js";
import rule10 from "../rules/no-invalid-label-refs.js";
import rule11 from "../rules/no-missing-atx-heading-space.js";
import rule12 from "../rules/no-missing-label-refs.js";
import rule13 from "../rules/no-missing-link-fragments.js";
import rule14 from "../rules/no-multiple-h1.js";
import rule15 from "../rules/no-reference-like-urls.js";
import rule16 from "../rules/no-reversed-media-syntax.js";
import rule17 from "../rules/no-space-in-emphasis.js";
import rule18 from "../rules/no-unused-definitions.js";
import rule19 from "../rules/require-alt-text.js";
import rule20 from "../rules/table-column-count.js";
export default {
    "fenced-code-language": /** @type {{meta: typeof rule0.meta; create: (context: unknown) => any}} */ (rule0),
    "fenced-code-meta": /** @type {{meta: typeof rule1.meta; create: (context: unknown) => any}} */ (rule1),
    "heading-increment": /** @type {{meta: typeof rule2.meta; create: (context: unknown) => any}} */ (rule2),
    "no-bare-urls": /** @type {{meta: typeof rule3.meta; create: (context: unknown) => any}} */ (rule3),
    "no-duplicate-definitions": /** @type {{meta: typeof rule4.meta; create: (context: unknown) => any}} */ (rule4),
    "no-duplicate-headings": /** @type {{meta: typeof rule5.meta; create: (context: unknown) => any}} */ (rule5),
    "no-empty-definitions": /** @type {{meta: typeof rule6.meta; create: (context: unknown) => any}} */ (rule6),
    "no-empty-images": /** @type {{meta: typeof rule7.meta; create: (context: unknown) => any}} */ (rule7),
    "no-empty-links": /** @type {{meta: typeof rule8.meta; create: (context: unknown) => any}} */ (rule8),
    "no-html": /** @type {{meta: typeof rule9.meta; create: (context: unknown) => any}} */ (rule9),
    "no-invalid-label-refs": /** @type {{meta: typeof rule10.meta; create: (context: unknown) => any}} */ (rule10),
    "no-missing-atx-heading-space": /** @type {{meta: typeof rule11.meta; create: (context: unknown) => any}} */ (rule11),
    "no-missing-label-refs": /** @type {{meta: typeof rule12.meta; create: (context: unknown) => any}} */ (rule12),
    "no-missing-link-fragments": /** @type {{meta: typeof rule13.meta; create: (context: unknown) => any}} */ (rule13),
    "no-multiple-h1": /** @type {{meta: typeof rule14.meta; create: (context: unknown) => any}} */ (rule14),
    "no-reference-like-urls": /** @type {{meta: typeof rule15.meta; create: (context: unknown) => any}} */ (rule15),
    "no-reversed-media-syntax": /** @type {{meta: typeof rule16.meta; create: (context: unknown) => any}} */ (rule16),
    "no-space-in-emphasis": /** @type {{meta: typeof rule17.meta; create: (context: unknown) => any}} */ (rule17),
    "no-unused-definitions": /** @type {{meta: typeof rule18.meta; create: (context: unknown) => any}} */ (rule18),
    "require-alt-text": /** @type {{meta: typeof rule19.meta; create: (context: unknown) => any}} */ (rule19),
    "table-column-count": /** @type {{meta: typeof rule20.meta; create: (context: unknown) => any}} */ (rule20),
};
