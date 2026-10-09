import type { ReactElement, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function icon(paths: ReactElement) {
  return function Icon({ size = 18, ...props }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        {...props}
      >
        {paths}
      </svg>
    );
  };
}

export const IconFeather = icon(
  <>
    <path d="M20.2 3.8c-5 .6-9.4 3.6-11.6 8.1L6 17.6" />
    <path d="M20.2 3.8c.2 5.3-2.6 10-7.5 12.2l-6.7 1.6" />
    <path d="M4 20l2-2.4" />
    <path d="M10.5 13.5h5" />
  </>,
);
export const IconUndo = icon(
  <>
    <path d="M9 14L4 9l5-5" />
    <path d="M4 9h10.5a5.5 5.5 0 010 11H11" />
  </>,
);
export const IconRedo = icon(
  <>
    <path d="M15 14l5-5-5-5" />
    <path d="M20 9H9.5a5.5 5.5 0 000 11H13" />
  </>,
);
export const IconText = icon(
  <>
    <path d="M4 7V5h16v2" />
    <path d="M12 5v14" />
    <path d="M9 19h6" />
  </>,
);
export const IconTextAdd = icon(
  <>
    <path d="M3 7V5h12v2" />
    <path d="M9 5v14" />
    <path d="M6.5 19h5" />
    <path d="M18 13v8M14 17h8" />
  </>,
);
export const IconDownload = icon(
  <>
    <path d="M12 4v11" />
    <path d="M7 10l5 5 5-5" />
    <path d="M5 20h14" />
  </>,
);
export const IconMinus = icon(<path d="M5 12h14" />);
export const IconPlus = icon(
  <>
    <path d="M12 5v14" />
    <path d="M5 12h14" />
  </>,
);
export const IconBold = icon(
  <>
    <path d="M7 5h6a3.5 3.5 0 010 7H7z" />
    <path d="M7 12h7a3.5 3.5 0 010 7H7z" />
  </>,
);
export const IconItalic = icon(
  <>
    <path d="M10 5h8" />
    <path d="M6 19h8" />
    <path d="M14 5l-4 14" />
  </>,
);
export const IconUnderline = icon(
  <>
    <path d="M7 4v7a5 5 0 0010 0V4" />
    <path d="M5 20h14" />
  </>,
);
export const IconStrike = icon(
  <>
    <path d="M5 12h14" />
    <path d="M16.5 7.5C16 5.6 14.2 4.5 12 4.5c-2.6 0-4.5 1.4-4.5 3.4 0 1.5 1 2.5 3 3.1" />
    <path d="M8 16.5c.6 1.9 2.3 3 4.2 3 2.7 0 4.6-1.4 4.6-3.5 0-.9-.3-1.6-1-2.2" />
  </>,
);
export const IconAlignLeft = icon(
  <>
    <path d="M4 6h16M4 10h10M4 14h16M4 18h10" />
  </>,
);
export const IconAlignCenter = icon(
  <>
    <path d="M4 6h16M7 10h10M4 14h16M7 18h10" />
  </>,
);
export const IconAlignRight = icon(
  <>
    <path d="M4 6h16M10 10h10M4 14h16M10 18h10" />
  </>,
);
export const IconAlignJustify = icon(
  <>
    <path d="M4 6h16M4 10h16M4 14h16M4 18h16" />
  </>,
);
export const IconTrash = icon(
  <>
    <path d="M4 7h16" />
    <path d="M10 11v6M14 11v6" />
    <path d="M6 7l1 13h10l1-13" />
    <path d="M9 7V4h6v3" />
  </>,
);
export const IconCheck = icon(<path d="M5 12.5l4.5 4.5L19 7.5" />);
export const IconX = icon(
  <>
    <path d="M6 6l12 12" />
    <path d="M18 6L6 18" />
  </>,
);
export const IconMove = icon(
  <>
    <path d="M12 3v18M3 12h18" />
    <path d="M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3" />
  </>,
);
export const IconUpload = icon(
  <>
    <path d="M12 16V4" />
    <path d="M7 9l5-5 5 5" />
    <path d="M5 20h14" />
  </>,
);
export const IconFile = icon(
  <>
    <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
    <path d="M14 3v5h5" />
  </>,
);
export const IconWarning = icon(
  <>
    <path d="M12 4l9 16H3z" />
    <path d="M12 10v4M12 17.5v.01" />
  </>,
);
export const IconPages = icon(
  <>
    <rect x="4" y="3" width="11" height="14" rx="1.5" />
    <path d="M9 21h9a2 2 0 002-2V8" />
  </>,
);
