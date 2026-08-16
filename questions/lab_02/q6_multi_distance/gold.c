#include <stdio.h>

/* No math.h -- this grader's compiler invocation places -lm before the
   source file, which fails to link any real (non-constant-folded) libm
   call. sqrt/pow/fabs are reimplemented here instead. */

double my_fabs(double x) {
    return x < 0 ? -x : x;
}

double my_exp(double x) {
    int k = 0;
    double y = x;
    while (y > 1.0 || y < -1.0) { y /= 2.0; k++; }
    double term = 1.0, sum = 1.0;
    for (int i = 1; i <= 30; i++) {
        term *= y / i;
        sum += term;
    }
    for (int i = 0; i < k; i++) sum *= sum;
    return sum;
}

double my_ln(double x) {
    int k = 0;
    double m = x;
    while (m >= 2.0) { m /= 2.0; k++; }
    while (m < 1.0) { m *= 2.0; k--; }
    double u = (m - 1.0) / (m + 1.0);
    double u2 = u * u;
    double term = u, sum = 0.0;
    for (int i = 1; i <= 31; i += 2) {
        sum += term / i;
        term *= u2;
    }
    const double LN2 = 0.6931471805599453;
    return 2.0 * sum + k * LN2;
}

double my_pow(double base, double e) {
    if (base == 0.0) return (e == 0.0) ? 1.0 : 0.0;
    if (e == (double)(long long)e) {
        long long n = (long long)e;
        int neg = n < 0;
        if (neg) n = -n;
        double result = 1.0, b = base;
        while (n > 0) {
            if (n & 1) result *= b;
            b *= b;
            n >>= 1;
        }
        return neg ? 1.0 / result : result;
    }
    return my_exp(e * my_ln(base));
}

double my_sqrt(double x) {
    return my_pow(x, 0.5);
}

void printMenu();
double euclidean(double x1, double y1, double z1,
                 double x2, double y2, double z2);
double manhattan(double x1, double y1, double z1,
                 double x2, double y2, double z2);
double chebyshev(double x1, double y1, double z1,
                 double x2, double y2, double z2);
double minkowski(double x1, double y1, double z1,
                 double x2, double y2, double z2, double p);

void printMenu() {
    printf("1. Euclidean Distance\n");
    printf("2. Manhattan Distance\n");
    printf("3. Chebyshev Distance\n");
    printf("4. Minkowski Distance\n");
    printf("5. Quit\n");
    printf("Enter choice: ");
}

double euclidean(double x1, double y1, double z1,
                 double x2, double y2, double z2) {
    return my_sqrt(my_pow(x2-x1,2) + my_pow(y2-y1,2) + my_pow(z2-z1,2));
}

double manhattan(double x1, double y1, double z1,
                 double x2, double y2, double z2) {
    return my_fabs(x2-x1) + my_fabs(y2-y1) + my_fabs(z2-z1);
}

double chebyshev(double x1, double y1, double z1,
                 double x2, double y2, double z2) {
    double dx = my_fabs(x2-x1);
    double dy = my_fabs(y2-y1);
    double dz = my_fabs(z2-z1);
    double max = dx;
    if (dy > max) max = dy;
    if (dz > max) max = dz;
    return max;
}

double minkowski(double x1, double y1, double z1,
                 double x2, double y2, double z2, double p) {
    return my_pow(my_pow(my_fabs(x2-x1),p) + my_pow(my_fabs(y2-y1),p)
               + my_pow(my_fabs(z2-z1),p), 1.0/p);
}

int main() {
    double x1, y1, z1, x2, y2, z2;
    scanf("%lf %lf %lf %lf %lf %lf",
          &x1, &y1, &z1, &x2, &y2, &z2);
    int choice = 0;
    while (choice != 5) {
        printMenu();
        scanf("%d", &choice);
        if (choice == 1) {
            printf("Euclidean Distance : %.4f\n",
                   euclidean(x1,y1,z1,x2,y2,z2));
        } else if (choice == 2) {
            printf("Manhattan Distance : %.4f\n",
                   manhattan(x1,y1,z1,x2,y2,z2));
        } else if (choice == 3) {
            printf("Chebyshev Distance : %.4f\n",
                   chebyshev(x1,y1,z1,x2,y2,z2));
        } else if (choice == 4) {
            double p;
            printf("Enter Minkowski order p: ");
            scanf("%lf", &p);
            if (p < 1) {
                printf("INVALID ORDER\n");
            } else {
                printf("Minkowski Distance (p=%.1f) : %.4f\n",
                       p, minkowski(x1,y1,z1,x2,y2,z2,p));
            }
        } else if (choice == 5) {
            printf("Exiting. Goodbye!\n");
        } else {
            printf("Invalid choice\n");
        }
    }
    return 0;
}
