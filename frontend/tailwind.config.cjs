const generatedTheme = require("./src/design/tailwind.theme.json");

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    colors: generatedTheme.colors,
    spacing: generatedTheme.spacing,
    borderRadius: generatedTheme.borderRadius,
    fontFamily: generatedTheme.fontFamily,
    fontSize: generatedTheme.fontSize,
    transitionDuration: generatedTheme.transitionDuration,
    transitionTimingFunction: generatedTheme.transitionTimingFunction,
    boxShadow: generatedTheme.boxShadow,
    extend: {
      maxWidth: {
        content: "1440px",
        readable: "72ch",
      },
    },
  },
  plugins: [],
};
