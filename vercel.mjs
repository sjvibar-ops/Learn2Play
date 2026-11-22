const environment = process.env.VERCEL_ENV;

if (environment !== 'preview' && environment !== 'production') {
  throw new Error('Set VERCEL_ENV to preview or production to configure Learn2Play routing.');
}

export const config = {
  version: 2,
  rewrites: [{
    source: '/:path*',
    destination: process.env.VERCEL_ENV === 'preview'
      ? 'https://learn2play-staging.onrender.com/:path*'
      : process.env.VERCEL_ENV === 'production'
        ? 'https://learn2play.onrender.com/:path*'
        : undefined,
  }],
};
