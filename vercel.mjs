const renderOrigin = process.env.RENDER_ORIGIN?.replace(/\/$/, "");

if (!renderOrigin || !renderOrigin.startsWith("https://")) {
  throw new Error(
    "Set RENDER_ORIGIN to the HTTPS URL of your Render service before deploying.",
  );
}

export const config = {
  rewrites: [
    {
      source: "/(.*)",
      destination: `${renderOrigin}/$1`,
    },
  ],
};
