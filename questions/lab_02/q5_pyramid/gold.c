#include <stdio.h>

int main() {
    int n;
    scanf("%d", &n);

    if (n <= 0) {
        printf("INVALID INPUT\n");
        return 0;
    }

    for (int i = 1; i <= n; i++) {
        for (int s = 1; s <= i - 1; s++)
            printf(" ");
        for (int c = 1; c <= 2 * (n - i) + 1; c++)
            printf("*");
        printf("\n");
    }

    return 0;
}
