const defaultTheme = require("tailwindcss/defaultTheme");
const colors = require("tailwindcss/colors");

module.exports = {
  content: ["./src/**/*.{js,jsx,ts,tsx}", "./app/templates/*.html"],
  theme: {
    colors: {
      pcatyellow: "#FDE021",
      pcatgray: "#00274D",
      pcatgray2: "#193F54",
      pcatgray3: "#94A0AA",
      pcatblue: "#008AD3",
      "pcatblue-400": "#005887",
      black: colors.black,
      white: colors.white,
      gray: colors.gray,
      emerald: colors.emerald,
      indigo: colors.indigo,
      yellow: colors.yellow,
      pink: colors.pink,
      blue: {
        50: "rgb(230,244,251)",
        100: "#e1f3fb",
        200: "#c4e7f6",
        300: "#7bc0e6",
        400: "#4ba8dd",
        500: "#2b95d6",
        600: "#008AD3",
        700: "#006bb3",
        800: "#004d80",
        900: "#003366",
      },
      red: colors.red,
      green: colors.green,
      orange: colors.orange,
    },
    extend: {
      fontFamily: {
        sans: ["Titillium Web", ...defaultTheme.fontFamily.sans],
        title: ["Orbitron"],
      },
    },
  },
  plugins: [require("@tailwindcss/forms")],
};
