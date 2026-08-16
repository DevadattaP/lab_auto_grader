#include<stdio.h>

int main()
{
    int a, b;
    char op;
    scanf("%d %d %c", &a, &b, &op);

    switch(op)
    {
        case '+':
            printf("%d", a+b);
            break;
        case '-':
            printf("%d", a-b);
            break;
        case '*':
            printf("%d", a*b);
            break;
        case '/':
            switch(b)
            {
                case 0:
                    printf("MATH ERROR");
                    break;
                default:
                    printf("%.2f", (float)a/b);
            }
            break;
        case '%':
            switch(b)
            {
                case 0:
                    printf("MATH ERROR");
                    break;
                default:
                    printf("%d", a%b);
            }
            break;
        default:
            printf("INVALID OPERATOR");
    }
    return 0;
}
