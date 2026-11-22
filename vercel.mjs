const environment = process.env.VERCEL_ENV;
let origin;

switch (environment) {
  case 'preview':
    origin = 'https://learn2play-staging.onrender.com';
    break;
  case 'production':
    origin = 'https://learn2play.onrender.com';
    break;
  default:
    throw new Error('Set VERCEL_ENV to preview or production to configure Learn2Play routing.');
}

export const config = {
  version: 2,
  rewrites: [{ source: '/:path*', destination: `${origin}/:path*` }],
};
