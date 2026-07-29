/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/axiom/static/index.html',
    './src/axiom/static/templates/**/*.html',
    './src/axiom/static/js/**/*.js',
    './src/axiom/static/tailwind.safelist.html',
  ],
  theme: {
    fontFamily: {
      sans: ['-apple-system', 'BlinkMacSystemFont', 'SF Pro Text', 'Helvetica Neue', 'sans-serif'],
    },
    extend: {
      colors: {
        brand: {
          50: '#EFF6FF', 100: '#DBEAFE', 200: '#BFDBFE', 300: '#93C5FD',
          400: '#60A5FA', 500: '#0071E3', 600: '#0064CC', 700: '#0055B3',
          800: '#004499', 900: '#003380',
        },
      },
    },
  },
  plugins: [],
};
